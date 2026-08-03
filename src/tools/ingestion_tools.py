"""
Custom MCP tools for the evidence pipeline.

Two tools are exposed:

1. ``update_localization`` — persists deep-search's AS-IS code observations
   back to the evidence cards.  Phase 16 redesign: each call is scoped to a
   ``scope_requirement_id``; the scope's previous entries are fully replaced
   (no merge, no dedup, no contradiction guard).  The scope's slice is stored
   on ``RequirementItem.scoped_evidence`` (the persisted per-requirement
   source); the top-level aggregate cards are rebuilt from every
   requirement's slice.

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
    RequirementItem,
    RequirementStatus,
    ScopedEvidence,
    StructuralCard,
)
from src.models.context import EvidenceCards
from src.models.memory import SharedWorkingMemory


# ── Module-level shared state ─────────────────────────────────────────────

_working_memory: SharedWorkingMemory | None = None
_evidence_json_path: Path | None = None
_repo_root: str = ""


_COMPLIANT_BLOCKER_RE = re.compile(
    r"traceability failure|unverifiable|must be verified|not backed by read|"
    r"cannot be confirmed|not confirmed by read|requires read re-verification|"
    r"not re-verified|inconclusive|self-reflection failure",
    re.IGNORECASE,
)


def _has_compliant_blocker(findings: str) -> bool:
    """Return True when findings explicitly say compliant is not verified."""
    return bool(_COMPLIANT_BLOCKER_RE.search(findings or ""))


def _short_reason(findings: str, limit: int = 240) -> str:
    """Compact a verified compliant finding for status storage."""
    text = re.sub(r"\s+", " ", (findings or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_compliant_requirements(evidence: EvidenceCards) -> None:
    """Migrate legacy compliant RequirementItems into lightweight status.

    Existing evidence artifacts may still carry AS_IS_COMPLIANT entries in the
    main requirements list.  Valid ones are moved to requirement_status; invalid
    ones are downgraded so the attribution/rework gates can reopen them.
    """
    active: list[RequirementItem] = []
    statuses = {
        status.id: status for status in evidence.requirement_status
    }
    for req in evidence.requirements:
        if req.verdict != "AS_IS_COMPLIANT":
            active.append(req)
            continue
        if (
            not req.evidence_locations
            or _has_compliant_blocker(req.findings)
            or req.origin == "new_interfaces"
        ):
            req.verdict = "TO_BE_PARTIAL"
            req.findings = (
                "AS_IS_COMPLIANT rejected during evidence normalization: "
                "compliant status requires traceable evidence_locations, "
                "no unverifiable/traceability-failure language, and "
                "non-new_interfaces origin.\n\n"
                f"{req.findings}"
            ).strip()
            active.append(req)
            continue
        statuses[req.id] = RequirementStatus(
            id=req.id,
            text=req.text,
            origin=req.origin,
            short_reason=_short_reason(req.findings),
            evidence_locations=list(req.evidence_locations),
            source_span=req.source_span,
            parent_contract_id=req.parent_contract_id,
            contract_kind=req.contract_kind,
            explicit_paths=list(req.explicit_paths),
            explicit_symbols=list(req.explicit_symbols),
            source_block_hash=req.source_block_hash,
        )
    evidence.requirements = active
    evidence.requirement_status = list(statuses.values())


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
    "consistency_anchors",
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
    "consistency_anchors",
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
    global _working_memory
    _normalize_compliant_requirements(evidence)
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
    _normalize_compliant_requirements(evidence)
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


def restore_working_memory(memory: SharedWorkingMemory) -> None:
    """Set working memory directly from a deserialized checkpoint.

    Used by run_pipeline_from_checkpoint to restore the full in-process
    state without re-running the parser or calling init_working_memory.
    """
    global _working_memory
    _working_memory = memory


def _format_previous_state(target: RequirementItem) -> str:
    """Render a requirement's about-to-be-cleared state as a 'rejected state'
    block, prepended to rework_context so the next deep-search iteration sees
    the prior (rejected) verdict/locations/findings as a negative example."""
    findings = (target.findings or "").strip()
    if len(findings) > 600:
        findings = findings[:597].rstrip() + "..."
    locs = ", ".join(target.evidence_locations) if target.evidence_locations else "(none)"
    return (
        "── PREVIOUS (REJECTED) STATE ──\n"
        f"verdict: {target.verdict}\n"
        f"evidence_locations: {locs}\n"
        f"findings (excerpt): {findings or '(none)'}\n"
        "──────────────────────────────"
    )


def reset_requirement_for_rework(
    requirement_id: str,
    audit_feedback: str = "",
    fields_to_reset: set[str] | None = None,
) -> bool:
    """Re-open a RequirementItem for a rework deep-search cycle.

    Always sets verdict back to UNCHECKED (otherwise the TODO picker will not
    re-dispatch deep-search for it).  Which other fields are cleared depends on
    *fields_to_reset*:

    - ``None`` (default, full reset): clears findings, evidence_locations, and
      the requirement's ``scoped_evidence`` slice (then rebuilds the aggregate
      view).  Used by the deterministic gates (attribution / anchor / I2 /
      static grounding) and by the ``deepen`` (sufficiency) and cross-req
      ``reconcile`` (consistency) rework operators.
    - ``{"findings"}``: clears findings ONLY, preserving evidence_locations and
      the scoped_evidence slice.  Used when the cited locations were verified
      OK but the findings text was a hallucination / failed the prescriptive
      boundary check — only the conclusion needs rewriting.

    Before clearing, the prior verdict / locations / findings are snapshotted
    into ``rework_context`` as a "previous rejected state" block so the next
    deep-search iteration can avoid repeating the same reasoning.

    *audit_feedback* — closure-checker (or gate) feedback specific to this
    requirement, appended after the snapshot block.  Deep-search is responsible
    for clearing rework_context after a new verdict is persisted.

    Returns True if the requirement existed and was reset.
    """
    if _working_memory is None:
        return False
    evidence = _working_memory.evidence_cards
    target = None
    for req in evidence.requirements:
        if req.id == requirement_id:
            target = req
            break

    if target is None:
        # Compliant requirement parked in requirement_status: re-materialize it
        # as a fresh active RequirementItem (always a full reset — no prior
        # active state to preserve).
        status = None
        for item in evidence.requirement_status:
            if item.id == requirement_id:
                status = item
                break
        if status is None:
            return False
        snapshot = (
            "── PREVIOUS (REJECTED) STATE ──\n"
            "verdict: AS_IS_COMPLIANT\n"
            f"evidence_locations: "
            f"{', '.join(status.evidence_locations) if status.evidence_locations else '(none)'}\n"
            f"findings (excerpt): {(status.short_reason or '(none)')}\n"
            "──────────────────────────────"
        )
        context = snapshot + (f"\n\n{audit_feedback}" if audit_feedback else "")
        target = RequirementItem(
            id=status.id,
            text=status.text,
            origin=status.origin,
            verdict="UNCHECKED",
            evidence_locations=[],
            findings="",
            scoped_evidence=ScopedEvidence(),
            rework_context=context,
            source_span=status.source_span,
            parent_contract_id=status.parent_contract_id,
            contract_kind=status.contract_kind,
            explicit_paths=list(status.explicit_paths),
            explicit_symbols=list(status.explicit_symbols),
            source_block_hash=status.source_block_hash,
        )
        evidence.requirements.append(target)
        evidence.requirement_status = [
            item for item in evidence.requirement_status if item.id != requirement_id
        ]
        _rebuild_aggregate_view()
        _persist_evidence_to_disk()
        return True

    snapshot = _format_previous_state(target)
    context = snapshot + (f"\n\n{audit_feedback}" if audit_feedback else "")

    target.verdict = "UNCHECKED"
    target.rework_context = context

    findings_only = fields_to_reset is not None and fields_to_reset == {"findings"}
    if findings_only:
        # Conclusion-only rework: keep verified locations and scoped slice.
        target.findings = ""
    else:
        # Full reset: drop findings, locations, and the scoped_evidence slice.
        target.findings = ""
        target.evidence_locations = []
        target.scoped_evidence = ScopedEvidence()
        _rebuild_aggregate_view()

    _persist_evidence_to_disk()
    return True


# ── Path normalization ────────────────────────────────────────────────────

# Recognize the path token at the start of an entry. Matches forward-slash /
# alphanumeric / dot / dash / underscore characters up to the first separator
# (colon for line/locator, whitespace, end of string).
_PATH_TOKEN_RE = re.compile(r"^([A-Za-z0-9_./-]+)(?=$|[:\s])")
# Same shape, but anchored after ` <-> ` so the RHS path of an anchor entry
# is also reachable. ``<->`` is the consistency-anchor separator.
_ANCHOR_RHS_RE = re.compile(r"(<->\s*)([A-Za-z0-9_./-]+)(?=$|[:\s])")
_MAX_STRIP_SEGMENTS = 6


def _strip_phantom_prefix(rel_path: str) -> str:
    """Drop hallucinated leading segments (e.g. ``app/``) until the path
    resolves under the configured repo root.

    LLMs occasionally prepend the cwd basename (``/app`` -> ``app/``) or
    similar synthetic prefixes when emitting evidence locations. This
    breaks every downstream step that resolves the path on disk
    (apply_search_replace, git diff, hash check). The fix is local to the
    path-ingestion choke point: if the verbatim path does not exist under
    repo root, peel one leading segment at a time and try again. Return
    the original input if no candidate resolves — never invent a path.
    """
    if not _repo_root or not rel_path:
        return rel_path
    repo = Path(_repo_root)
    if (repo / rel_path).exists():
        return rel_path
    parts = rel_path.split("/")
    for i in range(1, min(_MAX_STRIP_SEGMENTS, len(parts)) + 1):
        candidate = "/".join(parts[i:])
        if candidate and (repo / candidate).exists():
            return candidate
    return rel_path


def _normalize_path(text: str) -> str:
    """Normalize path references inside a free-form evidence string."""
    normalized = text.replace("\\", "/")
    if _repo_root:
        normalized = normalized.replace(_repo_root, "")
    normalized = re.sub(r"(?<![A-Za-z0-9_/])(?:\./)?repo/", "", normalized)
    # Phantom-prefix peel: applied to the leading path token and (when
    # present) the RHS path of a consistency anchor. Only the path portion
    # is touched; line/locator suffixes are preserved verbatim.
    m = _PATH_TOKEN_RE.match(normalized)
    if m:
        cleaned = _strip_phantom_prefix(m.group(1))
        if cleaned != m.group(1):
            normalized = cleaned + normalized[m.end():]
    normalized = _ANCHOR_RHS_RE.sub(
        lambda mm: mm.group(1) + _strip_phantom_prefix(mm.group(2)),
        normalized,
    )
    return normalized


# ── Scope → aggregate rebuild ─────────────────────────────────────────────

def _card_attr_for_field(field_name: str) -> str:
    """Return the ScopedEvidence card attribute ('localization' / 'structural'
    / 'constraint') that owns *field_name*."""
    if field_name in _LOCALIZATION_FIELDS:
        return "localization"
    if field_name in _STRUCTURAL_FIELDS:
        return "structural"
    return "constraint"


def _aggregate_field(field_name: str) -> list[str]:
    """Flatten every requirement's scoped_evidence contribution for one field,
    preserving requirements-list order and de-duplicating across requirements."""
    if _working_memory is None:
        return []
    card_attr = _card_attr_for_field(field_name)
    out: list[str] = []
    seen: set[str] = set()
    for req in _working_memory.evidence_cards.requirements:
        card = getattr(req.scoped_evidence, card_attr)
        for v in getattr(card, field_name, []):
            if v not in seen:
                out.append(v)
                seen.add(v)
    return out


def _rebuild_aggregate_view() -> None:
    """Rebuild LocalizationCard / StructuralCard / ConstraintCard (deep-search
    subset) from every requirement's scoped_evidence slice, preserving
    parser-owned fields intact."""
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
    global _working_memory

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

    # ── Locate the owning RequirementItem (scoped_evidence is the source) ──
    target = None
    for req in _working_memory.evidence_cards.requirements:
        if req.id == scope_id:
            target = req
            break
    if target is None:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: scope_requirement_id '{scope_id}' not found among "
                    f"active requirements "
                    f"{[r.id for r in _working_memory.evidence_cards.requirements]}. "
                    "Only active (non-compliant) requirements can receive "
                    "scoped observations."
                ),
            }]
        }

    # ── Rebuild this requirement's scoped slice (wholesale replace) ──
    new_scoped = ScopedEvidence()
    counts: dict[str, int] = {}
    for name in DEEP_SEARCH_OWNED_FIELDS:
        values = args.get(name)
        if values:
            normalized = [_normalize_path(v) for v in values]
            card = getattr(new_scoped, _card_attr_for_field(name))
            setattr(card, name, normalized)
            counts[name] = len(normalized)
    target.scoped_evidence = new_scoped

    _rebuild_aggregate_view()
    saved_msg = _persist_evidence_to_disk()

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
                "substantiating the verdict. Required for AS_IS_COMPLIANT, "
                "AS_IS_VIOLATED, and TO_BE_PARTIAL; TO_BE_MISSING may be "
                "empty when no definition exists."
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

    evidence = _working_memory.evidence_cards
    target = None
    target_index = -1
    for index, item in enumerate(evidence.requirements):
        if item.id == req_id:
            target = item
            target_index = index
            break
    if target is None:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: requirement_id '{req_id}' not found among "
                    f"{[r.id for r in evidence.requirements]}."
                ),
            }]
        }

    downgraded_compliant = False
    if verdict == "AS_IS_COMPLIANT":
        if (
            not evidence_locations
            or _has_compliant_blocker(findings)
            or target.origin == "new_interfaces"
        ):
            verdict = "TO_BE_PARTIAL"
            downgraded_compliant = True
            reason = (
                "AS_IS_COMPLIANT rejected: compliant requirements require "
                "traceable evidence_locations, must not contain unverifiable "
                "or traceability-failure language, and cannot originate from "
                "new_interfaces."
            )
            findings = f"{reason}\n\n{findings}".strip()

    if (
        verdict not in ("AS_IS_COMPLIANT", "TO_BE_MISSING")
        and not evidence_locations
        and not downgraded_compliant
    ):
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"ERROR: evidence_locations must be non-empty for "
                    f"verdict '{verdict}'."
                ),
            }]
        }

    if verdict == "AS_IS_COMPLIANT":
        status = RequirementStatus(
            id=target.id,
            text=target.text,
            origin=target.origin,
            short_reason=_short_reason(findings),
            evidence_locations=evidence_locations,
            source_span=target.source_span,
            parent_contract_id=target.parent_contract_id,
            contract_kind=target.contract_kind,
            explicit_paths=list(target.explicit_paths),
            explicit_symbols=list(target.explicit_symbols),
            source_block_hash=target.source_block_hash,
        )
        evidence.requirement_status = [
            item for item in evidence.requirement_status if item.id != req_id
        ]
        evidence.requirement_status.append(status)
        del evidence.requirements[target_index]
        # The requirement (and its scoped_evidence slice) is gone from the
        # active list; rebuild the aggregate view so its observations no longer
        # appear in the top-level cards.
        _rebuild_aggregate_view()
        target.rework_context = ""

        _working_memory.record_action(
            phase="deep-search",
            subagent="update_requirement_verdict",
            outcome=f"{verdict}:{len(evidence_locations)}_locs:moved_to_status",
            requirement_id=req_id,
        )
        saved_msg = _persist_evidence_to_disk()

        return {
            "content": [{
                "type": "text",
                "text": (
                    f"Requirement '{req_id}' -> AS_IS_COMPLIANT stored as "
                    f"lightweight status with {len(evidence_locations)} "
                    f"locations.{saved_msg}"
                ),
            }]
        }

    target.verdict = verdict
    target.evidence_locations = evidence_locations
    target.findings = findings
    evidence.requirement_status = [
        item for item in evidence.requirement_status if item.id != req_id
    ]
    # Consume any rework_context now that a fresh verdict has been recorded;
    # the audit feedback was meant to steer this single iteration only.
    target.rework_context = ""

    _working_memory.record_action(
        phase="deep-search",
        subagent="update_requirement_verdict",
        outcome=(
            f"{verdict}:{len(evidence_locations)}_locs"
            + (":downgraded_from_compliant" if downgraded_compliant else "")
        ),
        requirement_id=req_id,
    )
    saved_msg = _persist_evidence_to_disk()

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Requirement '{req_id}' -> {verdict} with "
                f"{len(evidence_locations)} locations"
                f"{' (downgraded from invalid AS_IS_COMPLIANT)' if downgraded_compliant else ''}."
                f"{saved_msg}"
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
