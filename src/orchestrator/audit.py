"""
Audit manifest builder (phase 18.B).

build_audit_manifest() produces a deterministic AuditManifest from evidence.
All audit scope decisions are made by code — not by LLM prompt rules.
"""

from __future__ import annotations

import re

from src.models.audit import AuditManifest, AuditTask, CheckType
from src.models.context import EvidenceCards

# Prescriptive keywords that signal a fix recommendation in findings.
_PRESCRIPTIVE_KEYWORDS = (
    "correct", "should be", "must be", "正确", "应改为",
    "instead of", "rather than", "the right", "the proper",
    "use X instead", "change to", "replace with",
)


def _has_backtick_snippet(findings: str) -> bool:
    """Return True if findings contains a backtick-enclosed code snippet."""
    return bool(re.search(r"`[^`]+`", findings))


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


def _locations_overlap(a: str, b: str) -> bool:
    """Return True if two evidence_locations reference the same file+line region."""
    path_a, start_a, end_a = _parse_evidence_location(a)
    path_b, start_b, end_b = _parse_evidence_location(b)
    if path_a != path_b:
        return False
    # Check range overlap: [start_a, end_a] ∩ [start_b, end_b] ≠ ∅
    return not (end_a < start_b or end_b < start_a)


def build_audit_manifest(
    evidence: EvidenceCards,
    structural_warnings: list[str] | None = None,
) -> AuditManifest:
    """Build a deterministic AuditManifest from current evidence.

    Rules (all code-driven):
    - Non-AS_IS_COMPLIANT reqs → always audited, full checks.
    - origin==new_interfaces reqs → always audited, verdict_vs_code + anti-hallucination.
    - AS_IS_COMPLIANT reqs with overlapping evidence_locations → audited (verdict_vs_code only).
    - Findings with backtick snippets → add findings_anti_hallucination.
    - Findings with prescriptive language (non-compliant reqs) → add prescriptive_boundary_self_check.

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
    seen_req_ids: set[str] = set()

    for req in evidence.requirements:
        reasons: list[str] = []
        checks: list[CheckType] = []
        cited = list(req.evidence_locations)

        # Rule 1: non-compliant → full checks
        if req.verdict not in ("AS_IS_COMPLIANT", "UNCHECKED"):
            reasons.append("non_compliant_defect")
            checks.append("verdict_vs_code")
            if _has_backtick_snippet(req.findings):
                checks.append("findings_anti_hallucination")
            if _has_prescriptive(req.findings):
                checks.append("prescriptive_boundary_self_check")

        # Rule 2: new_interfaces origin → always audit
        if req.origin == "new_interfaces":
            reasons.append("new_interface_origin")
            if "verdict_vs_code" not in checks:
                checks.append("verdict_vs_code")
            if _has_backtick_snippet(req.findings) and "findings_anti_hallucination" not in checks:
                checks.append("findings_anti_hallucination")

        # Rule 3: AS_IS_COMPLIANT with overlap → verdict_vs_code only
        if req.verdict == "AS_IS_COMPLIANT" and not reasons:
            # Check if any other req's locations overlap with this one's
            overlaps_others = False
            for other in evidence.requirements:
                if other.id == req.id:
                    continue
                for loc_a in cited:
                    for loc_b in other.evidence_locations:
                        if _locations_overlap(loc_a, loc_b):
                            overlaps_others = True
                            break
                    if overlaps_others:
                        break
                if overlaps_others:
                    break
            if overlaps_others:
                reasons.append("overlap_group")
                checks.append("verdict_vs_code")

        if reasons and req.id not in seen_req_ids:
            seen_req_ids.add(req.id)
            tasks.append(AuditTask(
                requirement_id=req.id,
                reasons=reasons,
                cited_locations=cited,
                checks_required=checks,
            ))

    return AuditManifest(
        tasks=tasks,
        warnings=list(structural_warnings) if structural_warnings else [],
    )
