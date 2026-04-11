"""
Custom MCP tools for the evidence pipeline:

1. update_localization — lets the Orchestrator persist confirmed code regions found
   by Deep Search back to the evidence_cards.json file on disk.
2. cache_retrieved_code — lets the Orchestrator cache critical code snippets
   found by Deep Search into SharedWorkingMemory.

Uses module-level SharedWorkingMemory state so the calling Python code can
retrieve results after the agent loop finishes.
"""

from pathlib import Path
import re
from typing import Any

from claude_agent_sdk import tool

from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    StructuralCard,
)
from src.models.context import EvidenceCards
from src.models.memory import SharedWorkingMemory

# Shared working memory; set by engine after parser returns,
# read/updated by MCP tools during the orchestrator loop.
_working_memory: SharedWorkingMemory | None = None

# Path where evidence_cards.json lives; set by the orchestrator engine before
# launching the orchestrator agent so update_localization can write it back.
_evidence_json_path: Path | None = None

# Repo root path; set by the orchestrator engine so we can strip absolute paths.
_repo_root: str = ""


def set_evidence_json_path(path: str | Path) -> None:
    global _evidence_json_path
    _evidence_json_path = Path(path)


def get_evidence_json_path() -> Path | None:
    return _evidence_json_path


def set_repo_root(path: str | Path) -> None:
    """Set the repo root so absolute paths can be normalized."""
    global _repo_root
    _repo_root = str(Path(path).resolve()).replace("\\", "/").rstrip("/") + "/"


def init_working_memory(
    issue_context: str, evidence: EvidenceCards
) -> SharedWorkingMemory:
    """Initialize the shared working memory (called by engine after parser)."""
    global _working_memory
    _working_memory = SharedWorkingMemory(
        issue_context=issue_context,
        evidence_cards=evidence,
    )
    return _working_memory


def get_working_memory() -> SharedWorkingMemory | None:
    return _working_memory


def get_submitted_evidence() -> EvidenceCards | None:
    """Backward-compatible accessor for evidence cards."""
    if _working_memory is not None:
        return _working_memory.evidence_cards
    return None


def set_submitted_evidence(evidence: EvidenceCards) -> None:
    """Backward-compatible setter — updates evidence_cards inside working memory."""
    global _working_memory
    if _working_memory is not None:
        _working_memory.evidence_cards = evidence
    else:
        _working_memory = SharedWorkingMemory(
            issue_context="(not set)",
            evidence_cards=evidence,
        )


def reset_submitted_evidence() -> None:
    global _working_memory
    _working_memory = None


# ── Path & text normalization helpers ────────────────────────────────────

def _normalize_path(text: str) -> str:
    """Normalize path references inside a free-form evidence string.

    Handles:
    - Backslashes → forward slashes
    - Absolute path prefixes (e.g. D:/demo/workdir/.../repo/...) → relative
    - Leading repo/ or ./ prefixes
    """
    normalized = text.replace("\\", "/")

    if _repo_root:
        normalized = normalized.replace(_repo_root, "")

    # Strip common prefix patterns
    normalized = re.sub(r"(?<![A-Za-z0-9_/])(?:\./)?repo/", "", normalized)

    return normalized


def _extract_location_key(text: str) -> str | None:
    """Extract the primary file:line location from an evidence entry.

    Returns a normalized 'file.py:N' or 'file.py:N-M' string, or None.
    """
    m = re.search(r"([A-Za-z0-9_/.]+\.py):(\d+(?:-\d+)?)", text)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None


def _dedup_by_location(entries: list[str]) -> list[str]:
    """Deduplicate entries that share the same primary file:line location.

    When multiple entries reference the same file:line, keeps the longest
    (most detailed) one.  Entries without a file:line are always kept.
    """
    by_loc: dict[str, str] = {}
    no_loc: list[str] = []

    for entry in entries:
        loc = _extract_location_key(entry)
        if loc is None:
            no_loc.append(entry)
        elif loc not in by_loc or len(entry) > len(by_loc[loc]):
            by_loc[loc] = entry

    # Preserve original order: walk entries, emit first occurrence per location
    seen_locs: set[str] = set()
    result: list[str] = []
    no_loc_set = set(no_loc)
    for entry in entries:
        loc = _extract_location_key(entry)
        if loc is None:
            if entry in no_loc_set:
                result.append(entry)
                no_loc_set.discard(entry)
        elif loc not in seen_locs:
            result.append(by_loc[loc])
            seen_locs.add(loc)

    return result


# ── Symbol extraction for contradiction guard ────────────────────────────

def _extract_primary_symbols(text: str) -> set[str]:
    """Extract primary callable/class identifiers from an evidence entry.

    Matches:
    - 'def X' / 'class X' patterns
    - 'X(args)' call patterns
    - '_method_name method/function' — common LLM phrasing for dead code entries
    """
    candidates: set[str] = set()
    value = text.strip()
    if not value:
        return candidates

    # 'def function_name'
    for match in re.findall(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\b", value):
        candidates.add(match.lower())

    # 'class ClassName'
    for match in re.findall(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", value):
        candidates.add(match.lower())

    # 'name(args)' — function call signatures
    for match in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(", value):
        low = match.lower()
        candidates.add(low)
        if "." in low:
            candidates.add(low.rsplit(".", 1)[-1])

    # '_method_name method|function|方法' — LLM often writes entries like
    # "_adjust_detection_parameters method in repo/..." for dead code.
    for match in re.findall(
        r"\b(_[A-Za-z_][A-Za-z0-9_]*)\s+(?:method|function|方法)\b", value
    ):
        candidates.add(match.lower())

    # 'set_xxx method|function|方法' — public setter-like methods
    for match in re.findall(
        r"\b(set_[A-Za-z_][A-Za-z0-9_]*)\s+(?:method|function|方法)\b", value
    ):
        candidates.add(match.lower())

    return candidates


# ── Merge helpers ────────────────────────────────────────────────────────

def _merge(existing: list[str], new: list[str]) -> list[str]:
    """Merge two lists, preserving order, deduplicating by exact string."""
    seen = set(existing)
    merged = list(existing)
    for v in new:
        if v not in seen:
            merged.append(v)
            seen.add(v)
    return merged


# ── MCP Tool Schemas ─────────────────────────────────────────────────────

_UPDATE_LOCALIZATION_SCHEMA = {
    "type": "object",
    "description": (
        "Persist confirmed defect locations AND structural/constraint findings "
        "from Deep Search back to evidence_cards.json.  Call this as soon as "
        "you extract findings from a Deep Search report — do NOT wait until "
        "final closure.  You MUST include ALL fields that Deep Search reported, "
        "not just exact_code_regions."
    ),
    "properties": {
        "exact_code_regions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exact line numbers or ranges in 'file.py:N' or 'file.py:N-M' form, "
                "e.g. ['backend/models/face_detector.py:120', "
                "'backend/models/face_detector.py:126']."
            ),
        },
        "suspect_entities": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files, classes, functions, or variables confirmed or updated "
                "by Deep Search."
            ),
        },
        "call_chain_context": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Caller-Callee chains discovered by Deep Search, formatted as "
                "'A -> B -> C' strings."
            ),
        },
        "dataflow_relevant_uses": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Def-Use relationships discovered by Deep Search."
            ),
        },
        "must_co_edit_relations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Co-edit dependencies discovered by Deep Search: 'If A changes "
                "→ B must also change'."
            ),
        },
        "dependency_propagation": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Cross-cutting dependency paths discovered by Deep Search "
                "(interface/package/config relationships)."
            ),
        },
        "missing_elements_to_implement": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "TO-BE elements confirmed absent from the codebase by Deep "
                "Search. These are interfaces/classes/methods required by "
                "specifications but not yet implemented."
            ),
        },
    },
    "required": [],
}


@tool(
    "update_localization",
    (
        "Persist confirmed defect locations and program-analysis context "
        "discovered by Deep Search into the evidence_cards.json on disk. "
        "You MUST call this tool before declaring evidence closure whenever "
        "Deep Search has identified concrete code regions."
    ),
    _UPDATE_LOCALIZATION_SCHEMA,
)
async def update_localization(args: dict[str, Any]) -> dict[str, Any]:
    """Write confirmed localization, structural, and constraint data back to
    the evidence JSON file."""
    print(f"[update_localization CALLED] args keys: {list(args.keys())}", flush=True)
    global _working_memory, _evidence_json_path

    if _working_memory is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No working memory initialized. Run the parser first.",
                }
            ]
        }

    evidence = _working_memory.evidence_cards

    # ── Normalize all incoming paths ──
    exact_code_regions = [_normalize_path(v) for v in args.get("exact_code_regions", [])]
    suspect_entities = [_normalize_path(v) for v in args.get("suspect_entities", [])]
    call_chain_context = [_normalize_path(v) for v in args.get("call_chain_context", [])]
    dataflow_relevant_uses = [_normalize_path(v) for v in args.get("dataflow_relevant_uses", [])]
    must_co_edit_relations = [_normalize_path(v) for v in args.get("must_co_edit_relations", [])]
    dependency_propagation = [_normalize_path(v) for v in args.get("dependency_propagation", [])]
    missing_elements = [_normalize_path(v) for v in args.get("missing_elements_to_implement", [])]

    # ── Merge new values into existing cards ──
    loc = evidence.localization
    merged_suspect = _merge(loc.suspect_entities, suspect_entities)
    merged_regions = _merge(loc.exact_code_regions, exact_code_regions)
    merged_chains = _merge(loc.call_chain_context, call_chain_context)
    merged_dataflow = _merge(loc.dataflow_relevant_uses, dataflow_relevant_uses)

    # ── Dedup by file:line location ──
    evidence.localization = LocalizationCard(
        suspect_entities=_dedup_by_location(merged_suspect),
        exact_code_regions=_dedup_by_location(merged_regions),
        call_chain_context=_dedup_by_location(merged_chains),
        dataflow_relevant_uses=_dedup_by_location(merged_dataflow),
    )

    if must_co_edit_relations or dependency_propagation:
        struc = evidence.structural
        evidence.structural = StructuralCard(
            must_co_edit_relations=_dedup_by_location(
                _merge(struc.must_co_edit_relations, must_co_edit_relations)
            ),
            dependency_propagation=_dedup_by_location(
                _merge(struc.dependency_propagation, dependency_propagation)
            ),
        )

    if missing_elements:
        con = evidence.constraint
        con.missing_elements_to_implement = _dedup_by_location(
            _merge(con.missing_elements_to_implement, missing_elements)
        )

    # ── Contradiction guard ──
    # Remove a missing_elements entry when its primary callable/class symbol
    # is ALSO found as a primary symbol in suspect_entities.
    con = evidence.constraint
    loc = evidence.localization
    suspect_symbols: set[str] = set()
    for entity in loc.suspect_entities:
        suspect_symbols.update(_extract_primary_symbols(entity))

    filtered_missing: list[str] = []
    removed_missing: list[str] = []
    for item in con.missing_elements_to_implement:
        item_symbols = _extract_primary_symbols(item)
        if item_symbols and item_symbols.issubset(suspect_symbols):
            removed_missing.append(item)
            continue
        filtered_missing.append(item)

    if removed_missing:
        con.missing_elements_to_implement = filtered_missing
        for item in removed_missing:
            print(
                "[contradiction resolved] removed from "
                f"missing_elements_to_implement (exists in suspect_entities): "
                f"{item[:120]}"
            )

    # Record action in working memory
    action_parts = []
    if exact_code_regions:
        action_parts.append(f"regions={len(exact_code_regions)}")
    if suspect_entities:
        action_parts.append(f"entities={len(suspect_entities)}")
    if call_chain_context:
        action_parts.append(f"chains={len(call_chain_context)}")
    _working_memory.record_action(
        f"update_localization: {', '.join(action_parts) or 'no new data'}"
    )

    if _evidence_json_path is not None:
        _evidence_json_path.write_text(
            evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        saved_msg = f" Saved to {_evidence_json_path}."
    else:
        saved_msg = " (evidence_json_path not set — in-memory only)"

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Evidence updated: exact_code_regions={len(exact_code_regions)}, "
                    f"entities={len(suspect_entities)}, "
                    f"call_chains={len(call_chain_context)}, "
                    f"dataflow={len(dataflow_relevant_uses)}, "
                    f"co_edits={len(must_co_edit_relations)}, "
                    f"dep_propagation={len(dependency_propagation)}, "
                    f"missing_elements={len(missing_elements)}.{saved_msg}"
                ),
            }
        ]
    }


_CACHE_CODE_SCHEMA = {
    "type": "object",
    "description": (
        "Cache a critical code snippet into the shared working memory. "
        "Use this when Deep Search has found code that is central to the "
        "defect (e.g. the buggy function body, a key caller, or a reference "
        "implementation).  Cached code is injected into downstream agents' "
        "context so they can reason about it without re-reading files."
    ),
    "required": ["location", "code"],
    "properties": {
        "location": {
            "type": "string",
            "description": (
                "Code location key in 'filepath:start_line-end_line' format, "
                "e.g. 'backend/models/face_detector.py:120-145'."
            ),
        },
        "code": {
            "type": "string",
            "description": "The actual source code text of the snippet.",
        },
    },
}


@tool(
    "cache_retrieved_code",
    (
        "Cache a critical code snippet found by Deep Search into the shared "
        "working memory.  Call this for code central to the defect — the "
        "buggy function, key callers, or reference implementations."
    ),
    _CACHE_CODE_SCHEMA,
)
async def cache_retrieved_code(args: dict[str, Any]) -> dict[str, Any]:
    """Store a code snippet in SharedWorkingMemory.retrieved_code."""
    global _working_memory

    if _working_memory is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No working memory initialized. Run the parser first.",
                }
            ]
        }

    location: str = _normalize_path(args["location"])
    code: str = args["code"]

    _working_memory.retrieved_code[location] = code
    _working_memory.record_action(f"cache_retrieved_code: {location}")

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Code cached: {location} "
                    f"({len(code)} chars, "
                    f"{len(_working_memory.retrieved_code)} total snippets)."
                ),
            }
        ]
    }
