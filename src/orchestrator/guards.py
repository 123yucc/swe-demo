"""
Mechanical guards and iteration budget for the orchestrator pipeline.

Phase 17 split of closure criteria into mechanical vs LLM-judged layers:

  * Sufficiency          — every RequirementItem has a non-UNCHECKED verdict
                           (code: ``check_sufficiency``).
  * Correct attribution  — every non-compliant verdict cites at least one
                           ``evidence_location`` AND every cited location has
                           legal ``path:LINE`` or ``path:LINE-LINE`` format
                           (code: ``check_correct_attribution``).  This guard
                           catches format / emptiness errors ONLY; it does
                           NOT judge whether the cited code actually supports
                           the verdict — factual audit is the closure-checker
                           LLM's job (phase 17.C).
  * Consistency + factual audit
                         — the closure-checker LLM opens each non-compliant
                           requirement's cited file regions and judges
                           whether the code actually supports the verdict.
                           See src/agents/closure_checker_agent.py.

Sufficiency and Correct attribution run BEFORE the closure-checker LLM; any
failure short-circuits the loop back to UNDER_SPECIFIED without wasting an
LLM invocation.
"""

from __future__ import annotations

import re

from src.models.context import EvidenceCards
from src.models.patch import PatchPlan


# evidence_location legal form: ``path:LINE`` or ``path:LINE-LINE``.
# The path may include directory separators and dots but must not be empty.
_EVIDENCE_LOCATION_RE = re.compile(r"^\S+?:\d+(?:-\d+)?$")

# ── Mechanical gates ─────────────────────────────────────────────────────


def check_sufficiency(evidence: EvidenceCards | None) -> list[str]:
    """Return requirement ids whose verdict is still UNCHECKED.

    If the returned list is non-empty, deep-search still has uncovered
    requirements and the pipeline must stay in UNDER_SPECIFIED.
    """
    if evidence is None:
        return ["<no evidence cards>"]
    return [r.id for r in evidence.requirements if r.verdict == "UNCHECKED"]


def check_correct_attribution(evidence: EvidenceCards | None) -> list[str]:
    """Return requirement ids that fail the mechanical attribution check.

    A non-compliant verdict (AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL)
    must:
      1. have a non-empty ``evidence_locations`` list, AND
      2. every entry must match ``path:LINE`` or ``path:LINE-LINE``.

    AS_IS_COMPLIANT is exempt — it's a negative finding and does not require
    a cited location.  UNCHECKED is skipped (sufficiency gate handles it).

    This guard is format-only; whether the cited code actually *supports*
    the verdict is judged by the closure-checker LLM with repo access.
    """
    if evidence is None:
        return []
    bad: list[str] = []
    for r in evidence.requirements:
        if r.verdict == "UNCHECKED":
            continue
        if r.verdict == "AS_IS_COMPLIANT":
            continue
        # TO_BE_MISSING describes absent functionality — the agent may not be
        # able to point to specific lines since the code doesn't exist yet.
        # Allow empty evidence_locations for this verdict.
        if r.verdict == "TO_BE_MISSING" and not r.evidence_locations:
            continue
        if not r.evidence_locations:
            bad.append(r.id)
            continue
        if any(not _EVIDENCE_LOCATION_RE.match(loc) for loc in r.evidence_locations):
            bad.append(r.id)
    return bad


def check_consistency_anchors_format(
    evidence: EvidenceCards | None,
) -> list[str]:
    """Return malformed anchor strings (format-only check).

    Each ``StructuralCard.consistency_anchors`` entry must split on ``<->``
    into two halves, each of which is ``path:locator`` with a non-empty path
    and locator. Factual verification (file exists, symbol/line resolves) is
    handled by ``check_consistency_anchors`` in
    ``src/orchestrator/consistency_checks.py``.

    This guard runs alongside ``check_correct_attribution`` — failures here
    bounce the pipeline back to UNDER_SPECIFIED without consulting the LLM.
    """
    if evidence is None:
        return []
    # Local import to avoid pulling subprocess/Path eagerly in guards.
    from src.orchestrator.consistency_checks import parse_anchor

    bad: list[str] = []
    for raw in evidence.structural.consistency_anchors:
        parsed = parse_anchor(raw)
        if parsed.parse_error:
            bad.append(f"{raw!r}: {parsed.parse_error}")
    return bad


# ── Iteration budget ──────────────────────────────────────────────────────


def check_plan_covers_violations(
    evidence: EvidenceCards | None,
    plan: PatchPlan | None,
) -> list[str]:
    """Return AS_IS_VIOLATED requirement ids whose cited files no edit touches.

    Spec-priority firewall (issue 011, req-005): a requirement whose text
    prescribes a change and whose verdict is AS_IS_VIOLATED owns a concrete
    change point at the location it cites. If the patch plan touches none of
    the files in that requirement's ``evidence_locations``, the prescribed fix
    is silently being skipped — usually because the deep-search findings
    rationalised the violation away as "a side-effect of another requirement"
    or "the cited code must remain unchanged". The plan must include the cited
    file so the change actually lands; otherwise the requirement is unaddressed
    no matter what the findings argue.

    Returns the list of uncovered requirement ids (empty = all covered). Only
    AS_IS_VIOLATED is checked: TO_BE_MISSING/PARTIAL may legitimately land in a
    new or different file, and AS_IS_COMPLIANT needs no change.
    """
    if evidence is None or plan is None:
        return []
    plan_paths = {
        e.filepath.replace("\\", "/").strip() for e in plan.edits if e.filepath
    }
    if not plan_paths:
        # No edits planned at all — every violated req is uncovered, but that
        # is a different failure (empty plan); don't double-report here.
        return []

    uncovered: list[str] = []
    for req in evidence.requirements:
        if req.verdict != "AS_IS_VIOLATED":
            continue
        cited_paths = {
            loc.split(":", 1)[0].replace("\\", "/").strip()
            for loc in req.evidence_locations
            if loc.strip()
        }
        if not cited_paths:
            continue  # attribution gate handles missing citations
        # Covered when any cited path is a planned edit (suffix-tolerant: the
        # plan may use a repo-relative path while the citation is identical).
        covered = any(
            cp == pp or cp.endswith("/" + pp) or pp.endswith("/" + cp)
            for cp in cited_paths
            for pp in plan_paths
        )
        if not covered:
            uncovered.append(req.id)
    return uncovered


def render_plan_coverage_feedback(
    evidence: EvidenceCards,
    uncovered_ids: list[str],
) -> str:
    """Render a planner-facing message naming the uncovered violated reqs."""
    if not uncovered_ids:
        return ""
    by_id = {r.id: r for r in evidence.requirements}
    lines = [
        "PLAN COVERAGE GAP — the following AS_IS_VIOLATED requirements cite a "
        "code location that NO planned edit touches. Each violated requirement "
        "owns a concrete change at its cited location; a fix that depends on "
        "another requirement's edit, or that argues the cited code should stay "
        "unchanged, does not satisfy it. Add an edit for the cited file(s):",
    ]
    for rid in uncovered_ids:
        req = by_id.get(rid)
        if req is None:
            continue
        locs = ", ".join(req.evidence_locations) or "(none)"
        lines.append(f"- {rid}: {req.text.strip()[:160]}")
        lines.append(f"    cited locations: {locs}")
    return "\n".join(lines)


# ── Iteration budget ──────────────────────────────────────────────────────


class DeepSearchBudget:
    """Track deep-search iteration count and enforce a maximum.

    Prevents infinite loops in the UnderSpecified <-> EvidenceRefining cycle.
    When the budget is exhausted, the orchestrator forces a single
    closure-checker evaluation and then terminates regardless of verdict.
    """

    def __init__(self, max_iterations: int = 5) -> None:
        self.max_iterations = max_iterations
        self.iteration = 0
        self._budget_exhausted = False

    def record_iteration(self) -> None:
        """Record one deep-search iteration."""
        self.iteration += 1
        print(
            f"[budget] deep-search iteration {self.iteration}/{self.max_iterations}",
            flush=True,
        )

    def is_exhausted(self) -> bool:
        """Return True if the iteration budget has been reached."""
        return self.iteration >= self.max_iterations

    def mark_budget_exhausted(self) -> None:
        """Mark that the budget was exhausted (for logging/outcome tracking)."""
        self._budget_exhausted = True

    @property
    def budget_exhausted(self) -> bool:
        return self._budget_exhausted


# ── Structural invariants (phase 18.A) ─────────────────────────────────────


def _extract_interface_names_from_text(text: str) -> list[str]:
    """Extract identifier-like tokens that look like interface/method names.

    Looks for tokens after common naming patterns: ``Name: X``, function calls,
    and bare CamelCase/snake_case identifiers.  Returns deduplicated list.
    """
    import re as _re

    # Pattern 1: "Name: X" or "interface: X" prefixes (new_interfaces format)
    prefix_pattern = _re.compile(
        r"(?:Name|Interface|Method|Function|API)\s*[:=]\s*([A-Za-z_][A-Za-z0-9_\.]*)",
        _re.IGNORECASE,
    )
    # Pattern 2: camelCase / PascalCase identifiers (at least 2 chars)
    identifier_pattern = _re.compile(r"\b([A-Z][a-zA-Z0-9]*(?:\.[A-Z][a-zA-Z0-9]*)*)\b")
    # Pattern 3: snake_case identifiers
    snake_pattern = _re.compile(r"\b([a-z_][a-z0-9_]*)\b")

    names: list[str] = []
    seen: set[str] = set()

    for m in prefix_pattern.finditer(text):
        token = m.group(1).strip()
        if token and token not in seen:
            seen.add(token)
            names.append(token)

    for m in identifier_pattern.finditer(text):
        token = m.group(1).strip()
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            names.append(token)

    for m in snake_pattern.finditer(text):
        token = m.group(1).strip()
        # Only include if it looks like a function/variable name (skip common keywords)
        if len(token) >= 3 and token not in seen and token not in {
            "function", "class", "import", "export", "return", "const",
            "async", "await", "default", "extends", "implements",
        }:
            seen.add(token)
            names.append(token)

    return names


def _keyword_overlap(text_a: str, text_b: str, min_shared: int = 2) -> bool:
    """Return True if texts share >= min_shared non-stopword tokens."""
    stopwords = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "shall", "can", "need", "to", "of",
        "in", "on", "at", "by", "for", "with", "without", "from", "as", "into",
        "through", "during", "before", "after", "above", "below", "between",
        "under", "again", "further", "then", "once", "here", "there", "when",
        "where", "why", "how", "all", "each", "few", "more", "most", "other",
        "some", "such", "only", "own", "same", "so", "than", "too", "very",
        "just", "and", "but", "or", "if", "not", "no", "nor", "for", "yet",
        "both", "either", "neither", "this", "that", "these", "those", "it",
        "its", "they", "them", "their", "we", "us", "our", "you", "your",
        "he", "him", "his", "she", "her", "what", "which", "who", "whom",
        "also", "like", "get", "set", "new", "up", "down", "out", "see",
        "make", "use", "add", "remove", "delete", "update", "change",
    }
    tokens_a = {
        t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text_a)
        if t.lower() not in stopwords and len(t) >= 3
    }
    tokens_b = {
        t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text_b)
        if t.lower() not in stopwords and len(t) >= 3
    }
    return len(tokens_a & tokens_b) >= min_shared


def check_structural_invariants(evidence: EvidenceCards) -> dict[str, list[str]]:
    """Check structural invariants across evidence cards.

    Returns a dict keyed by invariant name; each value is a list of failure
    descriptions.  Keys with empty lists passed.  The four invariants are:

    I1 — new_interface ↔ missing_elements bidirectional mapping:
      - Every req with origin==new_interfaces must have its interface name
        appear somewhere in constraint.missing_elements_to_implement.
      - Every line in missing_elements_to_implement must correspond to at
        least one origin==new_interfaces req (by name overlap).

    I2 — new_interface compliant is a contradiction:
      - Any req with origin==new_interfaces and verdict==AS_IS_COMPLIANT
        indicates deep-search hallucination; by definition a new interface
        does not exist yet.

    I3 — symptom → requirements coverage:
      - Each symptom.observable_failure should share >= 2 non-stopword
        tokens with at least one requirements-origin req text.

    I4 — declared-new-file co-edit completeness:
      - Any file path declared as NEW (keywords: "absent", "must be
        created", "does not exist", "missing", "new") in
        missing_elements_to_implement or suspect_entities must be
        mentioned at least once inside must_co_edit_relations or
        dependency_propagation.  A new file with no co-edit declaration
        means deep-search has not identified the file's integration
        points (callers, registries, mounts, imports); the planner will
        have nothing to backfill and the resulting patch will leave
        the new file unreachable.  Framework-agnostic — no assumption
        about directory layout, language, or registry conventions.

    I2 violations trigger immediate UNCHECKED reset (rework); I1/I3/I4 are
    warnings that flow into the closure-checker audit.
    """
    if evidence is None:
        return {}

    failures: dict[str, list[str]] = {
        "I1": [],
        "I2": [],
        "I3": [],
        "I4": [],
    }

    # ── Build name sets ──────────────────────────────────────────────────
    new_interface_reqs = [r for r in evidence.requirements if r.origin == "new_interfaces"]

    # For I1: map each new_interface req to its extracted names
    ni_req_names: dict[str, list[str]] = {}
    for req in new_interface_reqs:
        names = _extract_interface_names_from_text(req.text)
        ni_req_names[req.id] = names

    # For I1: extract names from missing_elements_to_implement
    missing_names: set[str] = set()
    for line in evidence.constraint.missing_elements_to_implement:
        for name in _extract_interface_names_from_text(line):
            missing_names.add(name)

    # ── I1 check ───────────────────────────────────────────────────────
    for req in new_interface_reqs:
        req_names = ni_req_names.get(req.id, [])
        for name in req_names:
            if name not in missing_names:
                failures["I1"].append(
                    f"I1_orphan_new_interface_req: {req.id} name={name!r}"
                )

    for line in evidence.constraint.missing_elements_to_implement:
        line_names = _extract_interface_names_from_text(line)
        matched = any(
            _keyword_overlap(req.text, line, min_shared=1)
            for req in new_interface_reqs
        )
        if line_names and not matched:
            failures["I1"].append(f"I1_orphan_missing_element: {line!r:.80}")

    # ── I2 check ────────────────────────────────────────────────────────
    for req in new_interface_reqs:
        if req.verdict == "AS_IS_COMPLIANT":
            names = ni_req_names.get(req.id, [])
            failures["I2"].append(
                f"I2_new_interface_cannot_be_compliant: {req.id} "
                f"(names={names})"
            )

    # ── I3 check ───────────────────────────────────────────────────────
    requirements_reqs = [r for r in evidence.requirements if r.origin == "requirements"]
    for failure in evidence.symptom.observable_failures:
        matched = any(
            _keyword_overlap(failure, req.text, min_shared=2)
            for req in requirements_reqs
        )
        if not matched:
            failures["I3"].append(f"I3_orphan_symptom: {failure!r:.80}")

    # ── I4 check (declared-new-file co-edit completeness) ─────────────
    # Framework-agnostic: any file path declared as NEW in
    # missing_elements_to_implement or suspect_entities ("absent", "must
    # be created", "does not exist", "new") should have at least one
    # sentence in must_co_edit_relations or dependency_propagation that
    # mentions its path — otherwise deep-search has not identified the
    # integration/registration points the new file depends on, and
    # downstream planning will be incomplete.
    file_path_re = re.compile(
        r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,6})\b"
    )
    new_keywords = ("absent", "must be created", "does not exist", "missing", " new ")

    # Corpus where new files are declared
    declared_sources: list[str] = (
        list(evidence.constraint.missing_elements_to_implement)
        + list(evidence.localization.suspect_entities)
    )
    # Corpus where co-edit dependencies are expected to appear
    coedit_corpus = "\n".join(
        list(evidence.structural.must_co_edit_relations)
        + list(evidence.structural.dependency_propagation)
    )

    seen_paths: set[str] = set()
    for sentence in declared_sources:
        # Only flag files that the sentence itself identifies as NEW.
        sentence_lower = " " + sentence.lower() + " "
        if not any(kw in sentence_lower for kw in new_keywords):
            continue
        for m in file_path_re.finditer(sentence):
            path = m.group(1).strip()
            if "/" not in path:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if path not in coedit_corpus:
                failures["I4"].append(
                    f"I4_orphan_new_file: new file {path!r} declared but no "
                    f"sentence in must_co_edit_relations or "
                    f"dependency_propagation references it; integration "
                    f"points are likely missing"
                )

    return failures
