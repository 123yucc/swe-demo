"""
Static evidence grounding gate (phase 25 — Correct Attribution, ③).

This module lowers the closure-checker's old FACTUAL audit (verdict_vs_code,
findings_anti_hallucination) from the LLM into a deterministic, file/grep-based
code gate. It answers "is each evidence item actually anchored in the
repository?" without any LLM call:

  * ground_exact_code_regions  — cited ``path:LINE[-LINE]`` regions resolve to a
                                 file and an in-bounds line range.
  * ground_suspect_entities    — ``path:symbol`` entries name a symbol that
                                 actually appears in that file.
  * ground_missing_elements    — backtick snippets in
                                 ``missing_elements_to_implement`` truly do NOT
                                 exist (presence contradicts the "missing"
                                 claim → fail).
  * ground_findings_snippets   — backtick snippets in a requirement's findings
                                 appear in at least one cited file.

Hard-fail discipline (mirrors build_verify's three-state lesson): a failure is
emitted ONLY on definite refutation — the file is missing, the line range is
out of bounds, the symbol/snippet is absent where it was claimed present, or a
"missing" element is in fact present. Anything the gate cannot parse or decide
is silently skipped (no fail), so brittleness never masquerades as a verdict.

``attribute_field_failure_to_req`` (gap B) maps a failed GLOBAL card-field entry
(symptom.*, constraint.* — which carry no requirement_id) back to the owning
requirement via a 3-tier reverse index (path → token → scoped_evidence),
falling back to ``"<global>"`` exactly like consistency_checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.models.context import EvidenceCards
from src.orchestrator.audit import _parse_evidence_location
from src.orchestrator.ast_grounding import (
    build_symbol_index,
    has_call_edge,
    has_exception_class,
    has_symbol_def,
)


# ── Result type ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroundingFailure:
    """One definite grounding refutation, attributed to a requirement.

    ``requirement_id`` is ``"<global>"`` when the failed item belongs to a
    global card and could not be attributed to a specific requirement.

    ``grounded_by`` records the provenance of the check that produced the
    refutation, unified with the phase-26 dynamic tags so a downstream reader
    can tell HOW each piece of evidence was grounded:
      * ``static_grep`` — file/line/word-boundary grep (regions, symbols,
        findings, missing elements).
      * ``ast``         — structural AST query (call edges, symptom symbols).
    """

    requirement_id: str
    kind: str  # region_oob | symbol_absent | missing_element_present | finding_snippet_absent
    detail: str
    matched_by: str = ""  # path | token | scoped | global (for attributed failures)
    grounded_by: str = "static_grep"  # static_grep | ast

    def render(self) -> str:
        return f"{self.requirement_id}: [{self.kind}] {self.detail}"


# ── File / symbol helpers ──────────────────────────────────────────────────

def _read_file(repo_dir: Path, rel_path: str) -> str | None:
    """Return file text under repo_dir, or None if it does not exist / unreadable."""
    fp = repo_dir / rel_path
    if not fp.is_file():
        return None
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _line_count(content: str) -> int:
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def _symbol_present(content: str, name: str) -> bool:
    """Word-boundary search for an identifier (same rule as consistency_checks)."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return pattern.search(content) is not None


_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _backtick_snippets(text: str) -> list[str]:
    """Extract backtick-enclosed snippets worth grounding (>= 3 chars)."""
    out: list[str] = []
    for raw in _BACKTICK_RE.findall(text or ""):
        s = raw.strip()
        if len(s) >= 3:
            out.append(s)
    return out


def _suspect_symbol(entry: str) -> tuple[str, str] | None:
    """Parse a ``path:symbol`` suspect-entity entry.

    Returns ``(path, symbol)`` only when the part after the last colon is a
    non-numeric identifier (a line number is a region, not a symbol → None).
    Entries without a colon are bare symbols with no file to anchor against →
    None (skipped, not failed).
    """
    if ":" not in entry:
        return None
    path, _, locator = entry.rpartition(":")
    path = path.strip()
    locator = locator.strip()
    if not path or not locator:
        return None
    # A numeric / range locator is a code region, handled elsewhere.
    if re.fullmatch(r"\d+(?:-\d+)?", locator):
        return None
    # Only ground clean identifiers; skip expressions / signatures.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", locator):
        return None
    return path, locator


# ── Per-requirement grounders ──────────────────────────────────────────────

def ground_exact_code_regions(
    requirement_id: str,
    regions: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """Each ``path:LINE[-LINE]`` must resolve to a file with an in-bounds range."""
    failures: list[GroundingFailure] = []
    for region in regions:
        path, start, end = _parse_evidence_location(region)
        if end is None or start == 0:
            # Not a parseable line region — skip (no opinion).
            continue
        content = _read_file(repo_dir, path)
        if content is None:
            failures.append(GroundingFailure(
                requirement_id=requirement_id,
                kind="region_oob",
                detail=f"cited region {region!r}: file {path!r} not found",
            ))
            continue
        lc = _line_count(content)
        if start < 1 or end > lc:
            failures.append(GroundingFailure(
                requirement_id=requirement_id,
                kind="region_oob",
                detail=(
                    f"cited region {region!r}: lines {start}-{end} out of "
                    f"bounds (file has {lc} lines)"
                ),
            ))
    return failures


def ground_suspect_entities(
    requirement_id: str,
    entities: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """``path:symbol`` entries must name a symbol present in that file."""
    failures: list[GroundingFailure] = []
    for entry in entities:
        parsed = _suspect_symbol(entry)
        if parsed is None:
            continue
        path, symbol = parsed
        content = _read_file(repo_dir, path)
        if content is None:
            # File absent — let region grounding own file-existence failures;
            # a suspect entity may legitimately point at a not-yet-created file.
            continue
        if not _symbol_present(content, symbol):
            failures.append(GroundingFailure(
                requirement_id=requirement_id,
                kind="symbol_absent",
                detail=f"suspect entity {entry!r}: symbol {symbol!r} not found in {path}",
            ))
    return failures


def ground_findings_snippets(
    requirement_id: str,
    findings: str,
    cited_locations: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """Backtick snippets in findings must appear in at least one cited file.

    Lowers the closure-checker's old ``findings_anti_hallucination`` check.
    Only runs when the requirement cites file locations to search; with no
    cited files there is nothing to refute against (skip).
    """
    snippets = _backtick_snippets(findings)
    if not snippets:
        return []
    cited_paths: list[str] = []
    seen: set[str] = set()
    for loc in cited_locations:
        path, _, _ = _parse_evidence_location(loc)
        if path and path not in seen:
            seen.add(path)
            cited_paths.append(path)
    if not cited_paths:
        return []

    contents: list[str] = []
    for path in cited_paths:
        c = _read_file(repo_dir, path)
        if c is not None:
            contents.append(c)
    if not contents:
        # None of the cited files are readable here — region grounding will
        # have flagged missing files; do not double-fail on snippets.
        return []

    failures: list[GroundingFailure] = []
    for snippet in snippets:
        if not any(snippet in c for c in contents):
            failures.append(GroundingFailure(
                requirement_id=requirement_id,
                kind="finding_snippet_absent",
                detail=(
                    f"findings cites `{snippet}` but it is absent from cited "
                    f"file(s) {cited_paths}"
                ),
            ))
    return failures


def ground_missing_elements(
    elements: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """Backtick snippets in ``missing_elements_to_implement`` must NOT exist.

    A snippet declared "missing" that grep finds in the codebase is a
    contradiction (the new_interface judgement is wrong). Returns failures
    tagged ``"<global>"`` — the caller attributes them to a requirement via
    ``attribute_field_failure_to_req``.

    Restricted to backtick snippets: only an explicitly-quoted, specific symbol
    is a defensible "this exact thing" signal. Free-text words are skipped to
    avoid false positives.
    """
    failures: list[GroundingFailure] = []
    # Search source files only; data/doc files are not definition sites.
    code_files = _collect_code_files(repo_dir)
    for line in elements:
        for snippet in _backtick_snippets(line):
            # Only ground clean identifiers — expressions/signatures are noisy.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", snippet):
                continue
            for content in code_files:
                if _symbol_present(content, snippet):
                    failures.append(GroundingFailure(
                        requirement_id="<global>",
                        kind="missing_element_present",
                        detail=(
                            f"missing_element `{snippet}` is declared absent but "
                            f"a definition/reference exists in the repository"
                        ),
                    ))
                    break
    return failures


_CODE_SUFFIXES = (".py", ".go", ".js", ".ts", ".jsx", ".tsx", ".java", ".rb", ".rs")
_SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}


def _collect_code_files(repo_dir: Path, limit: int = 4000) -> list[str]:
    """Read up to *limit* source files' contents for repo-wide symbol checks."""
    out: list[str] = []
    for fp in repo_dir.rglob("*"):
        if len(out) >= limit:
            break
        if not fp.is_file():
            continue
        if fp.suffix.lower() not in _CODE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in fp.parts):
            continue
        try:
            out.append(fp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return out


# ── AST-backed structural grounders (gap A) ────────────────────────────────

# Caller -> Callee arrow conventions used in call_chain_context entries.
_ARROW_RE = re.compile(r"\s*(?:->|→|=>)\s*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _chain_path_and_steps(entry: str) -> tuple[str | None, list[str]]:
    """Split a call_chain entry into an optional ``path:`` prefix + step names.

    Accepts ``file.py: A -> B -> C`` or bare ``A -> B``. Steps are reduced to
    their last identifier (``pkg.Fn`` → ``Fn``). Returns (path | None, steps).
    """
    text = entry.strip()
    path: str | None = None
    # A leading ``path.ext:`` prefix (not an arrow) names the file to parse.
    m = re.match(r"^([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,6}):\s*(.*)$", text)
    if m:
        path, text = m.group(1), m.group(2)
    raw_steps = _ARROW_RE.split(text)
    steps: list[str] = []
    for raw in raw_steps:
        ids = _IDENT_RE.findall(raw)
        if ids:
            steps.append(ids[-1])
    return path, steps


def ground_call_chain(
    requirement_id: str,
    chains: list[str],
    cited_locations: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """Verify ``Caller -> Callee`` edges via AST; soft-pass when AST unavailable.

    A fail is emitted ONLY when AST parses the relevant file and definitively
    shows no call edge between two consecutive named steps. If no file can be
    parsed (no path prefix and no parseable cited file, unsupported language,
    parse failure), the edge is left ungrounded (soft pass, no failure).
    """
    failures: list[GroundingFailure] = []
    # Candidate files: a chain's own path prefix, else the requirement's cited files.
    cited_paths = _distinct_paths(cited_locations)

    for entry in chains:
        path, steps = _chain_path_and_steps(entry)
        if len(steps) < 2:
            continue
        search_paths = [path] if path else cited_paths
        indexes = _indexes_for(search_paths, repo_dir)
        if not indexes:
            continue  # AST unavailable → soft pass
        for caller, callee in zip(steps, steps[1:]):
            # An edge is grounded if ANY indexed file shows it. Only fail when
            # at least one index defines the caller yet none show the edge.
            caller_defined = any(
                any(d.name == caller for d in idx.defs) for idx in indexes
            )
            edge_found = any(has_call_edge(idx, caller, callee) for idx in indexes)
            if caller_defined and not edge_found:
                failures.append(GroundingFailure(
                    requirement_id=requirement_id,
                    kind="call_edge_absent",
                    detail=(
                        f"call_chain {entry!r}: no call to {callee!r} found "
                        f"inside {caller!r}'s body"
                    ),
                    grounded_by="ast",
                ))
    return failures


def ground_symptom_symbols(
    symptom_failures: list[str],
    repo_dir: Path,
) -> list[GroundingFailure]:
    """Symbols / exception types named in observable_failures must have a def.

    Extracts backtick identifiers and ``XxxError`` / ``XxxException`` tokens; if
    AST can parse at least one repo file and NONE define the symbol, it is a
    hallucinated reference → fail (tagged ``<global>`` for attribution). If no
    file is parseable (AST unavailable repo-wide), soft pass.
    """
    indexes = _repo_indexes(repo_dir)
    if not indexes:
        return []  # AST unavailable → soft pass

    failures: list[GroundingFailure] = []
    seen: set[str] = set()
    for line in symptom_failures:
        candidates = list(_backtick_snippets(line))
        candidates += re.findall(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b", line)
        for name in candidates:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            if name in seen:
                continue
            seen.add(name)
            is_exc = name.endswith(("Error", "Exception"))
            check = has_exception_class if is_exc else has_symbol_def
            if not any(check(idx, name) for idx in indexes):
                # Exception classes may come from stdlib/builtins; only fail on
                # custom-looking names with no def anywhere in the repo.
                failures.append(GroundingFailure(
                    requirement_id="<global>",
                    kind="symptom_symbol_absent",
                    detail=(
                        f"observable_failure references {name!r} but no "
                        f"definition point exists in the repository"
                    ),
                    grounded_by="ast",
                ))
    return failures


def _distinct_paths(locations: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for loc in locations:
        path, _, _ = _parse_evidence_location(loc)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _indexes_for(paths: list[str | None], repo_dir: Path):
    """Build symbol indexes for the given repo-relative paths (skip Nones)."""
    indexes = []
    for path in paths:
        if not path:
            continue
        content = _read_file(repo_dir, path)
        if content is None:
            continue
        idx = build_symbol_index(path, content)
        if idx is not None:
            indexes.append(idx)
    return indexes


def _repo_indexes(repo_dir: Path, limit: int = 1200):
    """Build symbol indexes across repo source files (Python-first, bounded)."""
    indexes = []
    for fp in repo_dir.rglob("*"):
        if len(indexes) >= limit:
            break
        if not fp.is_file() or fp.suffix.lower() not in _CODE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in fp.parts):
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(fp.relative_to(repo_dir)).replace("\\", "/")
        idx = build_symbol_index(rel, content)
        if idx is not None:
            indexes.append(idx)
    return indexes


# ── Top-level static grounding gate ────────────────────────────────────────

def run_static_grounding(
    evidence: EvidenceCards | None,
    repo_dir: Path | None,
) -> list[GroundingFailure]:
    """Run every static grounding check over the current evidence.

    Per-requirement checks (region / suspect-symbol / findings-snippet) are
    attributed directly to the owning requirement. Global missing_element
    failures are attributed via ``attribute_field_failure_to_req``.

    Returns all definite refutations; an empty list means nothing was refuted.
    """
    if evidence is None or repo_dir is None:
        return []
    repo_dir = Path(repo_dir)
    failures: list[GroundingFailure] = []

    for req in evidence.requirements:
        if req.verdict == "UNCHECKED":
            continue
        loc = req.scoped_evidence.localization
        failures.extend(
            ground_exact_code_regions(req.id, loc.exact_code_regions, repo_dir)
        )
        failures.extend(
            ground_suspect_entities(req.id, loc.suspect_entities, repo_dir)
        )
        failures.extend(
            ground_findings_snippets(
                req.id, req.findings, req.evidence_locations, repo_dir
            )
        )
        failures.extend(
            ground_call_chain(
                req.id, loc.call_chain_context, req.evidence_locations, repo_dir
            )
        )

    # Global: symptom symbols (parser-owned observable_failures).
    for gf in ground_symptom_symbols(
        evidence.symptom.observable_failures, repo_dir
    ):
        rid, matched_by = attribute_field_failure_to_req(gf.detail, evidence)
        failures.append(GroundingFailure(
            requirement_id=rid, kind=gf.kind, detail=gf.detail,
            matched_by=matched_by, grounded_by=gf.grounded_by,
        ))

    # Global: missing_elements_to_implement (parser-owned, no req scope).
    for gf in ground_missing_elements(
        evidence.constraint.missing_elements_to_implement, repo_dir
    ):
        rid, matched_by = attribute_field_failure_to_req(gf.detail, evidence)
        failures.append(GroundingFailure(
            requirement_id=rid,
            kind=gf.kind,
            detail=gf.detail,
            matched_by=matched_by,
            grounded_by=gf.grounded_by,
        ))

    return failures


# ── Gap B: global card-field → requirement reverse attribution ─────────────

def attribute_field_failure_to_req(
    failure_text: str,
    evidence: EvidenceCards,
) -> tuple[str, str]:
    """Attribute a global card-field failure to the requirement it stems from.

    3-tier match (first hit wins), reusing existing conventions:
      1. path overlap  — a ``path:line`` token in the text matched against
                         requirements' evidence_locations paths.
      2. token overlap — guards._keyword_overlap (min_shared=2) of the failure
                         text against each req's ``text + findings``.
      3. scoped lookup — the text content appears in some req's scoped_evidence
                         localization/constraint/structural slice.

    Returns ``(requirement_id, matched_by)`` where matched_by ∈
    {path, token, scoped, global}; ``"<global>"`` when nothing matches.
    """
    # ── Tier 1: path overlap ──────────────────────────────────────────────
    path_to_reqs: dict[str, list[str]] = {}
    for req in evidence.requirements:
        for loc in req.evidence_locations:
            p = loc.split(":", 1)[0].strip()
            if p:
                path_to_reqs.setdefault(p, []).append(req.id)
    for m in re.finditer(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,6}):\d+", failure_text):
        p = m.group(1)
        if p in path_to_reqs:
            return path_to_reqs[p][0], "path"

    # ── Tier 2: token overlap ─────────────────────────────────────────────
    from src.orchestrator.guards import _keyword_overlap

    best_id = ""
    for req in evidence.requirements:
        haystack = f"{req.text}\n{req.findings}"
        if _keyword_overlap(failure_text, haystack, min_shared=2):
            best_id = req.id
            break
    if best_id:
        return best_id, "token"

    # ── Tier 3: scoped_evidence content reverse lookup ────────────────────
    for req in evidence.requirements:
        se = req.scoped_evidence
        slices: list[str] = []
        for card in (se.localization, se.constraint, se.structural):
            for field_values in card.model_dump().values():
                if isinstance(field_values, list):
                    slices.extend(str(v) for v in field_values)
        for snippet in _backtick_snippets(failure_text):
            if any(snippet in s for s in slices):
                return req.id, "scoped"

    return "<global>", "global"
