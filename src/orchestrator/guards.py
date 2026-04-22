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
        if not r.evidence_locations:
            bad.append(r.id)
            continue
        if any(not _EVIDENCE_LOCATION_RE.match(loc) for loc in r.evidence_locations):
            bad.append(r.id)
    return bad


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
    descriptions.  Keys with empty lists passed.  The three invariants are:

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

    I2 violations trigger immediate UNCHECKED reset (rework); I1/I3 are
    warnings that flow into the closure-checker audit.
    """
    if evidence is None:
        return {}

    failures: dict[str, list[str]] = {
        "I1": [],
        "I2": [],
        "I3": [],
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

    return failures
