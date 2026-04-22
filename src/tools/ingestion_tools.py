"""
Custom MCP tools for the evidence pipeline.

Two tools are exposed:

1. ``update_localization`` — persists deep-search's AS-IS code observations
   back to the evidence cards.  Phase 16 redesign: each call is scoped to a
   ``scope_requirement_id``; the scope's previous entries are fully replaced
   (no merge, no dedup, no contradiction guard).

2. ``cache_retrieved_code`` — caches critical code snippets in
   SharedWorkingMemory for downstream agents.

3. ``update_requirement_verdict`` — persists the verdict /
   evidence_locations / findings for a specific RequirementItem.

Module-level SharedWorkingMemory is used so the calling Python code can
inspect the results after an agent loop finishes.
"""

from __future__ import annotations

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


# ── Module-level shared state ─────────────────────────────────────────────

_working_memory: SharedWorkingMemory | None = None
_evidence_json_path: Path | None = None
_repo_root: str = ""

# Scope-based store for deep-search-owned fields.  Outer key: requirement id;
# inner key: evidence-card field name; value: the full list of entries that
# scope contributed (replaced wholesale on every update_localization call).
_scoped_store: dict[str, dict[str, list[str]]] = {}


# ── Field ownership tables (single source of truth) ──────────────────────

# Fields deep-search may write (scope-keyed).  Any other key sent by
# update_localization is a whitelist violation.
DEEP_SEARCH_OWNED_FIELDS: tuple[str, ...] = (
    "suspect_entities",
    "exact_code_regions",
    "call_chain_context",
    "dataflow_relevant_uses",
    "must_co_edit_relations",
    "dependency_propagation",
    "behavioral_constraints",
    "semantic_boundaries",
    "backward_compatibility",
    "similar_implementation_patterns",
)

# Fields deep-search MUST NOT overwrite — always owned by parser.
PARSER_OWNED_FIELDS: tuple[str, ...] = (
    "observable_failures",
    "repair_targets",
    "regression_expectations",
    "missing_elements_to_implement",
    "requirements",
)

_LOCALIZATION_FIELDS = (
    "suspect_entities",
    "exact_code_regions",
    "call_chain_context",
    "dataflow_relevant_uses",
)
_STRUCTURAL_FIELDS = (
    "must_co_edit_relations",
    "dependency_propagation",
)
_CONSTRAINT_FIELDS = (
    "behavioral_constraints",
    "semantic_boundaries",
    "backward_compatibility",
    "similar_implementation_patterns",
)


# ── Accessors / setters ───────────────────────────────────────────────────

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
    """Initialize shared working memory (called by engine after parser)."""
    global _working_memory, _scoped_store
    _working_memory = SharedWorkingMemory(
        issue_context=issue_context,
        evidence_cards=evidence,
    )
    _scoped_store = {}
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
    global _working_memory, _scoped_store
    _working_memory = None
    _scoped_store = {}


def reset_requirement_for_rework(
    requirement_id: str,
    audit_feedback: str = "",
) -> bool:
    """Re-open a RequirementItem for a rework deep-search cycle.

    Sets verdict back to UNCHECKED, clears its findings / evidence_locations,
    and drops its scope in _scoped_store so stale localization entries do not
    contaminate the next investigation.  Rebuilds the aggregate view.

    *audit_feedback* — optional closure-checker audit text specific to this
    requirement. Stored on ``RequirementItem.rework_context`` so the next
    deep-search iteration can read it and steer its reasoning.  Deep-search
    is responsible for clearing the field after a new verdict is persisted.

    Returns True if the requirement existed and was reset.
    """
    global _scoped_store
    if _working_memory is None:
        return False
    evidence = _working_memory.evidence_cards
    target = None
    for req in evidence.requirements:
        if req.id == requirement_id:
            target = req
            break
    if target is None:
        return False
    target.verdict = "UNCHECKED"
    target.evidence_locations = []
    target.findings = ""
    target.rework_context = audit_feedback or ""
    if requirement_id in _scoped_store:
        del _scoped_store[requirement_id]
        _rebuild_aggregate_view()
    return True


# ── Path normalization ────────────────────────────────────────────────────

def _normalize_path(text: str) -> str:
    """Normalize path references inside a free-form evidence string."""
    normalized = text.replace("\\", "/")
    if _repo_root:
        normalized = normalized.replace(_repo_root, "")
    normalized = re.sub(r"(?<![A-Za-z0-9_/])(?:\./)?repo/", "", normalized)
    return normalized


# ── Scope → aggregate rebuild ─────────────────────────────────────────────

def _aggregate_field(field_name: str) -> list[str]:
    """Flatten all scopes' contributions for one field, preserving scope
    insertion order."""
    out: list[str] = []
    seen: set[str] = set()
    for scope_entries in _scoped_store.values():
        for v in scope_entries.get(field_name, []):
            if v not in seen:
                out.append(v)
                seen.add(v)
    return out


def _rebuild_aggregate_view() -> None:
    """Rebuild LocalizationCard / StructuralCard / ConstraintCard (deep-search
    subset) from the scoped store, preserving parser-owned fields intact."""
    if _working_memory is None:
        return
    evidence = _working_memory.evidence_cards

    evidence.localization = LocalizationCard(
        **{name: _aggregate_field(name) for name in _LOCALIZATION_FIELDS}
    )
    evidence.structural = StructuralCard(
        **{name: _aggregate_field(name) for name in _STRUCTURAL_FIELDS}
    )

    # Constraint: preserve parser-owned missing_elements_to_implement;
    # rebuild the deep-search-owned subset.
    preserved_missing = list(evidence.constraint.missing_elements_to_implement)
    evidence.constraint = ConstraintCard(
        missing_elements_to_implement=preserved_missing,
        **{name: _aggregate_field(name) for name in _CONSTRAINT_FIELDS},
    )


def _persist_evidence_to_disk() -> str:
    if _evidence_json_path is None:
        return " (evidence_json_path not set — in-memory only)"
    if _working_memory is None:
        return ""
    _evidence_json_path.write_text(
        _working_memory.evidence_cards.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return f" Saved to {_evidence_json_path}."


# ── update_localization ───────────────────────────────────────────────────

_UPDATE_LOCALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Persist AS-IS code observations from the deep-search agent. "
        "Each call is scoped to a single requirement; the scope's previous "
        "entries are fully REPLACED (no merge)."
    ),
    "required": ["scope_requirement_id"],
    "properties": {
        "scope_requirement_id": {
            "type": "string",
            "description": (
                "RequirementItem.id that this batch of observations belongs to "
                "(e.g. 'req-003'). Entries written under this scope REPLACE "
                "any previous entries for the same scope."
            ),
        },
        **{
            name: {"type": "array", "items": {"type": "string"}}
            for name in DEEP_SEARCH_OWNED_FIELDS
        },
    },
}


@tool(
    "update_localization",
    (
        "Persist AS-IS code observations found by deep-search.  Must pass "
        "scope_requirement_id; each call REPLACES the scope's previous "
        "entries (no merge).  symptom.* and constraint.missing_elements_to_"
        "implement are parser-only and cannot be written here."
    ),
    _UPDATE_LOCALIZATION_SCHEMA,
)
async def update_localization(args: dict[str, Any]) -> dict[str, Any]:
    print(f"[update_localization CALLED] args keys: {list(args.keys())}", flush=True)
    global _working_memory, _scoped_store

    if _working_memory is None:
        return {
            "content": [{
                "type": "text",
                "text": "ERROR: No working memory initialized. Run the parser first.",
            }]
        }

    scope_id = args.get("scope_requirement_id")
    if not scope_id or not isinstance(scope_id, str):
        return {
            "content": [{
                "type": "text",
                "text": (
                    "ERROR: scope_requirement_id (str) is required — "
                    "pass the RequirementItem.id this batch belongs to."
                ),
            }]
        }

    # ── Whitelist check — parser-owned fields are forbidden here ──
    forbidden = [k for k in args.keys()
                 if k != "scope_requirement_id" and k in PARSER_OWNED_FIELDS]
    if forbidden:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: fields {forbidden} are parser-owned and cannot "
                    "be written via update_localization. Use update_"
                    "requirement_verdict for requirement state, and parser "
                    "owns symptom.* / missing_elements_to_implement."
                ),
            }]
        }

    unknown = [k for k in args.keys()
               if k != "scope_requirement_id"
               and k not in DEEP_SEARCH_OWNED_FIELDS]
    if unknown:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: unknown fields {unknown}. Allowed keys: "
                    f"scope_requirement_id, {list(DEEP_SEARCH_OWNED_FIELDS)}."
                ),
            }]
        }

    # ── Build fresh scope bucket (wholesale replace) ──
    new_bucket: dict[str, list[str]] = {}
    for name in DEEP_SEARCH_OWNED_FIELDS:
        values = args.get(name)
        if values:
            new_bucket[name] = [_normalize_path(v) for v in values]

    _scoped_store[scope_id] = new_bucket
    _rebuild_aggregate_view()
    saved_msg = _persist_evidence_to_disk()

    counts = {k: len(v) for k, v in new_bucket.items()}
    total = sum(counts.values()) if counts else 0
    _working_memory.record_action(
        phase="deep-search",
        subagent="update_localization",
        outcome=f"{total}_entries" if total else "cleared",
        requirement_id=scope_id,
    )

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Scope '{scope_id}' updated: {counts or 'cleared'}."
                f"{saved_msg}"
            ),
        }]
    }


# ── update_requirement_verdict ────────────────────────────────────────────

_UPDATE_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Persist deep-search's verdict for one RequirementItem.  Replaces "
        "the item's verdict / evidence_locations / findings wholesale."
    ),
    "required": ["requirement_id", "verdict"],
    "properties": {
        "requirement_id": {
            "type": "string",
            "description": "RequirementItem.id (e.g. 'req-003').",
        },
        "verdict": {
            "type": "string",
            "enum": [
                "UNCHECKED",
                "AS_IS_COMPLIANT",
                "AS_IS_VIOLATED",
                "TO_BE_MISSING",
                "TO_BE_PARTIAL",
            ],
        },
        "evidence_locations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Code locations ('file.py:LINE' or 'file.py:LINE-LINE') "
                "substantiating the verdict. Non-empty unless "
                "verdict == 'AS_IS_COMPLIANT'."
            ),
        },
        "findings": {
            "type": "string",
            "description": "Deep-search's on-site summary for this requirement.",
        },
    },
}


@tool(
    "update_requirement_verdict",
    (
        "Update one RequirementItem after deep-search has verified it "
        "against the code.  Sets verdict / evidence_locations / findings."
    ),
    _UPDATE_REQUIREMENT_SCHEMA,
)
async def update_requirement_verdict(args: dict[str, Any]) -> dict[str, Any]:
    print(
        f"[update_requirement_verdict CALLED] "
        f"req={args.get('requirement_id')} verdict={args.get('verdict')}",
        flush=True,
    )
    global _working_memory

    if _working_memory is None:
        return {
            "content": [{
                "type": "text",
                "text": "ERROR: No working memory initialized. Run the parser first.",
            }]
        }

    req_id = args.get("requirement_id")
    verdict = args.get("verdict")
    if not req_id or not verdict:
        return {
            "content": [{
                "type": "text",
                "text": "ERROR: requirement_id and verdict are both required.",
            }]
        }

    evidence_locations = [_normalize_path(v) for v in args.get("evidence_locations", [])]
    findings = args.get("findings", "") or ""

    target = None
    for item in _working_memory.evidence_cards.requirements:
        if item.id == req_id:
            target = item
            break
    if target is None:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: requirement_id '{req_id}' not found among "
                    f"{[r.id for r in _working_memory.evidence_cards.requirements]}."
                ),
            }]
        }

    if verdict != "AS_IS_COMPLIANT" and not evidence_locations:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: evidence_locations must be non-empty for "
                    f"verdict '{verdict}'."
                ),
            }]
        }

    target.verdict = verdict
    target.evidence_locations = evidence_locations
    target.findings = findings
    # Consume any rework_context now that a fresh verdict has been recorded;
    # the audit feedback was meant to steer this single iteration only.
    target.rework_context = ""

    _working_memory.record_action(
        phase="deep-search",
        subagent="update_requirement_verdict",
        outcome=f"{verdict}:{len(evidence_locations)}_locs",
        requirement_id=req_id,
    )
    saved_msg = _persist_evidence_to_disk()

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Requirement '{req_id}' -> {verdict} with "
                f"{len(evidence_locations)} locations.{saved_msg}"
            ),
        }]
    }


# ── cache_retrieved_code ──────────────────────────────────────────────────

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
            "content": [{
                "type": "text",
                "text": "ERROR: No working memory initialized. Run the parser first.",
            }]
        }

    location: str = _normalize_path(args["location"])
    code: str = args["code"]

    _working_memory.retrieved_code[location] = code
    _working_memory.record_action(
        phase="deep-search",
        subagent="cache_retrieved_code",
        outcome=f"cached:{location}",
    )

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Code cached: {location} "
                f"({len(code)} chars, "
                f"{len(_working_memory.retrieved_code)} total snippets)."
            ),
        }]
    }
