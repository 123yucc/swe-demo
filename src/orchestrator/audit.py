"""
Audit manifest builder (phase 18.B; re-scoped phase 25).

build_audit_manifest() produces a deterministic AuditManifest from evidence.
All audit scope decisions are made by code — not by LLM prompt rules.

Phase 25 re-scope: the closure-checker no longer performs the factual checks
(verdict_vs_code, findings_anti_hallucination) — those were lowered into the
deterministic grounding gate (grounding.py + ast_grounding.py). The only
per-task LLM check left is ``prescriptive_boundary_self_check``, so the
manifest now contains a task ONLY for requirements whose findings carry
prescriptive fix language. AS_IS_COMPLIANT requirements no longer live in
``evidence.requirements`` (they were moved to ``requirement_status``), so the
old overlap-group rule is gone.
"""

from __future__ import annotations

import re

from src.models.audit import AuditManifest, AuditTask, CheckType
from src.models.context import EvidenceCards


def _has_prescriptive(findings: str) -> bool:
    """Return True if findings contains prescriptive fix language.

    Uses context-aware patterns to distinguish:
    - Observational: "code does X instead of Y" (describing current behavior)
    - Prescriptive: "must use X instead of Y" (proposing a fix)
    """
    # Pattern 1: Modal verbs (must/should/need to) + action verbs
    # Matches: "must be changed", "should use X", "need to replace", "must return"
    pattern1 = r'\b(must|should|need to|correct is|the right)\b.{0,50}\b(be|use|change|replace|add|remove|fix|return)\b'

    # Pattern 2: Comparative phrases with modal context
    # Matches: "must return 400 instead of 404", "should be Y rather than X"
    pattern2 = r'\b(must|should|need to|correct)\b.{0,50}\b(instead of|rather than)\b'

    # Pattern 3: Explicit fix/solution language
    # Matches: "fix: use X", "solution: change Y", "correct approach: Z"
    pattern3 = r'\b(fix|solution|correct approach|the right way):\s*\w+'

    # Pattern 4: Imperative prescriptive phrases
    # Matches: "change to X", "replace with Y", "use X instead"
    pattern4 = r'\b(change to|replace with|use .* instead)\b'

    # Pattern 5: "correct/right X is Y" pattern
    # Matches: "correct status code is 400", "right approach is X"
    pattern5 = r'\b(correct|right)\b.{0,30}\b(is|should be|must be)\b'

    prescriptive_patterns = [pattern1, pattern2, pattern3, pattern4, pattern5]
    return any(re.search(pat, findings, re.IGNORECASE) for pat in prescriptive_patterns)


def _parse_evidence_location(loc: str) -> tuple[str, int, int | None]:
    """Parse 'path/to/file.py:L' or 'path/to/file.py:L-R' into (path, start, end)."""
    colon_idx = loc.rfind(":")
    if colon_idx == -1:
        return loc, 0, None
    path = loc[:colon_idx]
    rest = loc[colon_idx + 1:]
    if "-" in rest:
        parts = rest.split("-", 1)
        start = int(parts[0])
        end = int(parts[1])
        return path, start, end
    else:
        return path, int(rest), int(rest)


def build_audit_manifest(
    evidence: EvidenceCards,
    structural_warnings: list[str] | None = None,
) -> AuditManifest:
    """Build a deterministic AuditManifest from current evidence.

    Phase 25: the manifest now drives only the LLM's
    ``prescriptive_boundary_self_check``. A task is emitted for each active
    (non-UNCHECKED) requirement whose findings contain prescriptive fix
    language. All factual checks were lowered into the grounding gate.

    Args:
        evidence: Current EvidenceCards state.
        structural_warnings: I1/I3/I4 failure messages from check_structural_invariants
            to include as warnings in the manifest.

    Returns:
        AuditManifest with tasks list and optional warnings.
    """
    if evidence is None:
        return AuditManifest()

    tasks: list[AuditTask] = []
    for req in evidence.requirements:
        if req.verdict == "UNCHECKED":
            continue
        if not _has_prescriptive(req.findings):
            continue
        checks: list[CheckType] = ["prescriptive_boundary_self_check"]
        tasks.append(AuditTask(
            requirement_id=req.id,
            reasons=["findings_has_prescriptive"],
            cited_locations=list(req.evidence_locations),
            checks_required=checks,
        ))

    return AuditManifest(
        tasks=tasks,
        warnings=_build_warnings(evidence, structural_warnings),
    )


def _build_warnings(
    evidence: EvidenceCards,
    structural_warnings: list[str] | None,
) -> list[str]:
    """Compose the manifest warnings list.

    Includes I1/I3/I4 structural-invariant failures (informational for the
    closure-checker) and one ``anchor:`` line per declared consistency anchor.
    Anchors are *not* re-checked by the closure-checker — they are enforced
    by the consistency code gate in ``src/orchestrator/consistency_checks.py``.
    Surfacing them here lets the LLM cross-reference its reading against the
    declared invariants and flag any contradiction it spots while auditing
    other checks.
    """
    out: list[str] = list(structural_warnings) if structural_warnings else []
    for raw in evidence.structural.consistency_anchors:
        if raw.strip():
            out.append(f"anchor: {raw.strip()}")
    return out
