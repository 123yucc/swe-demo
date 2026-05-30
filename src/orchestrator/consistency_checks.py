"""
Consistency code gates (phase 23).

Two deterministic checks complement the closure-checker LLM and PatchVerifying:

1. ``check_consistency_anchors`` (closure stage)
   Each anchor in ``StructuralCard.consistency_anchors`` declares two endpoints
   that must be jointly consistent. The gate parses the anchor, opens the
   referenced file regions, and verifies each endpoint resolves to existing
   code. A pure file/grep verification — no LLM. Failures feed back as
   per-requirement rework_context, mirroring the closure-checker EVIDENCE_MISSING
   path.

2. ``check_rename_residue`` (PatchVerifying stage)
   After SEARCH/REPLACE edits land, the gate diffs the working tree against
   ``base_commit``, infers ``(old_name, new_name)`` rename candidates from line
   pairings, and greps the repo for surviving references to each old symbol.
   Hits in unmodified files indicate an under-propagated rename. Results plug
   into ``memory.build_error_feedback`` so the existing repatch loop carries
   them.

Neither gate consults the agent-invisible evaluator-injected test fixtures —
they only see ``base_commit`` files plus the patched working tree.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.models.context import EvidenceCards
from src.orchestrator.build_verify import BuildError


# ── Anchor parsing ─────────────────────────────────────────────────────────

_ANCHOR_SPLIT_RE = re.compile(r"\s*<-+>\s*")
_LINE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")
_SYMBOL_PREFIXES = (
    "class", "func", "function", "method", "type", "enum",
    "field", "name", "key", "interface", "struct", "var", "const",
)


@dataclass(frozen=True)
class AnchorEndpoint:
    """One side of a ``<lhs> <-> <rhs>`` consistency anchor."""

    raw: str
    path: str
    locator: str  # ``LINE``, ``LINE-LINE``, or ``symbol:NAME`` / ``NAME``

    @property
    def kind(self) -> str:
        if _LINE_RANGE_RE.match(self.locator):
            return "line"
        return "symbol"

    @property
    def line_range(self) -> tuple[int, int] | None:
        m = _LINE_RANGE_RE.match(self.locator)
        if not m:
            return None
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        return start, end

    @property
    def symbol_name(self) -> str:
        """Extract the bare identifier from ``symbol:NAME`` or ``NAME``."""
        s = self.locator
        for prefix in _SYMBOL_PREFIXES:
            head = prefix + ":"
            if s.lower().startswith(head):
                return s[len(head):].strip()
            head_eq = prefix + "="
            if s.lower().startswith(head_eq):
                return s[len(head_eq):].strip()
        if ":" in s:
            return s.split(":", 1)[1].strip()
        if "=" in s:
            return s.split("=", 1)[1].strip()
        return s.strip()


@dataclass(frozen=True)
class ParsedAnchor:
    raw: str
    lhs: AnchorEndpoint | None
    rhs: AnchorEndpoint | None
    parse_error: str = ""


def _parse_endpoint(text: str) -> AnchorEndpoint | None:
    """Split ``path:locator`` into an AnchorEndpoint. Empty/malformed → None."""
    s = text.strip().strip("`'\"")
    if not s or ":" not in s:
        return None
    path, locator = s.split(":", 1)
    path = path.strip()
    locator = locator.strip()
    if not path or not locator:
        return None
    return AnchorEndpoint(raw=s, path=path, locator=locator)


def parse_anchor(line: str) -> ParsedAnchor:
    """Parse one anchor string. Always returns ParsedAnchor (parse_error set on failure)."""
    raw = line.strip()
    if not raw:
        return ParsedAnchor(raw=raw, lhs=None, rhs=None, parse_error="empty")
    parts = _ANCHOR_SPLIT_RE.split(raw, maxsplit=1)
    if len(parts) != 2:
        return ParsedAnchor(
            raw=raw, lhs=None, rhs=None,
            parse_error="missing '<->' separator",
        )
    lhs_text, rhs_text = parts
    # Trim trailing parenthetical commentary like " (note: ...)" before parsing rhs.
    rhs_text = re.sub(r"\s+\([^)]*\)\s*$", "", rhs_text)
    lhs = _parse_endpoint(lhs_text)
    rhs = _parse_endpoint(rhs_text)
    if lhs is None or rhs is None:
        which = []
        if lhs is None:
            which.append("LHS")
        if rhs is None:
            which.append("RHS")
        return ParsedAnchor(
            raw=raw, lhs=lhs, rhs=rhs,
            parse_error=f"could not parse {' and '.join(which)} as 'path:locator'",
        )
    return ParsedAnchor(raw=raw, lhs=lhs, rhs=rhs)


# ── Anchor verification (closure stage) ───────────────────────────────────

@dataclass(frozen=True)
class AnchorFailure:
    """One failed consistency anchor."""

    requirement_id: str
    anchor: str
    reason: str

    def render(self) -> str:
        return f"{self.requirement_id}: anchor {self.anchor!r} — {self.reason}"


def _verify_endpoint(endpoint: AnchorEndpoint, repo_dir: Path) -> str | None:
    """Return failure reason string, or None if endpoint resolves."""
    file_path = repo_dir / endpoint.path
    if not file_path.is_file():
        return f"file {endpoint.path!r} not found"

    if endpoint.kind == "line":
        lr = endpoint.line_range
        if lr is None:
            return f"could not parse line range {endpoint.locator!r}"
        start, end = lr
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"could not read {endpoint.path}: {exc}"
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        if start < 1 or end > line_count:
            return (
                f"line range {start}-{end} out of bounds "
                f"(file has {line_count} lines)"
            )
        return None

    # Symbol kind: verify the identifier appears in the file (word boundary).
    name = endpoint.symbol_name
    if not name:
        return f"empty symbol name in locator {endpoint.locator!r}"
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"could not read {endpoint.path}: {exc}"
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    if not pattern.search(content):
        return f"symbol {name!r} not found in {endpoint.path}"
    return None


def check_consistency_anchors(
    evidence: EvidenceCards | None,
    repo_dir: Path | None,
) -> list[AnchorFailure]:
    """Verify every consistency anchor resolves on both ends.

    Returns failures (empty list = all anchors valid). When ``evidence`` is
    None or there are no anchors declared, returns an empty list (no-op).

    Each failure is annotated with the requirement_id whose evidence_locations
    overlap the LHS path; if no requirement matches by path, ``"<global>"`` is
    used so the orchestrator can still surface the failure.
    """
    if evidence is None or repo_dir is None:
        return []
    anchors = list(evidence.structural.consistency_anchors)
    if not anchors:
        return []

    repo_dir = Path(repo_dir)
    failures: list[AnchorFailure] = []

    # Build path → requirement_ids index from evidence_locations.
    path_to_reqs: dict[str, list[str]] = {}
    for req in evidence.requirements:
        for loc in req.evidence_locations:
            p = loc.split(":", 1)[0].strip()
            if p:
                path_to_reqs.setdefault(p, []).append(req.id)

    for raw in anchors:
        parsed = parse_anchor(raw)
        if parsed.parse_error:
            failures.append(AnchorFailure(
                requirement_id="<global>",
                anchor=raw,
                reason=parsed.parse_error,
            ))
            continue
        assert parsed.lhs is not None and parsed.rhs is not None
        lhs_err = _verify_endpoint(parsed.lhs, repo_dir)
        rhs_err = _verify_endpoint(parsed.rhs, repo_dir)
        if lhs_err is None and rhs_err is None:
            continue
        owning_reqs = (
            path_to_reqs.get(parsed.lhs.path, [])
            or path_to_reqs.get(parsed.rhs.path, [])
        )
        owner = owning_reqs[0] if owning_reqs else "<global>"
        reason_parts = []
        if lhs_err:
            reason_parts.append(f"LHS: {lhs_err}")
        if rhs_err:
            reason_parts.append(f"RHS: {rhs_err}")
        failures.append(AnchorFailure(
            requirement_id=owner,
            anchor=raw,
            reason="; ".join(reason_parts),
        ))

    return failures


# ── Rename-residue verification (PatchVerifying stage) ────────────────────

# Match a Go/Python/JS-style identifier, length ≥ 3 to avoid noise.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")

# Identifiers we never treat as renames — language keywords / common tokens.
_RENAME_BLACKLIST: frozenset[str] = frozenset({
    "func", "function", "class", "import", "from", "return", "const", "let",
    "var", "if", "else", "elif", "for", "while", "switch", "case", "break",
    "continue", "default", "type", "interface", "struct", "package", "module",
    "export", "default", "async", "await", "yield", "public", "private",
    "protected", "static", "final", "abstract", "extends", "implements",
    "true", "false", "null", "nil", "None", "True", "False", "self", "this",
    "super", "new", "delete", "in", "is", "not", "and", "or", "with", "as",
    "try", "catch", "except", "finally", "raise", "throw", "throws", "void",
    "int", "string", "bool", "float", "double", "char", "byte", "long",
    "short", "list", "dict", "tuple", "set", "map", "array", "vector",
    "error", "Error", "Exception", "json", "JSON", "url", "URL", "http",
    "Read", "Write", "Open", "Close", "Get", "Set", "Add", "New", "Make",
    "println", "print", "log", "Logger", "ctx", "context", "Context",
    "args", "kwargs", "params", "options", "config", "Config", "setup",
})

# Files whose contents are data, docs, or front-end UI strings — never the
# definition site of a backend symbol that just got renamed. Any "hit" in
# these is a string-collision, not a missed rename. The set is intentionally
# generous; the cost of skipping a true positive in a data file is low,
# while the noise from a single i18n collision can mask real residues.
_DATA_FILE_SUFFIXES: frozenset[str] = frozenset({
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".html", ".htm", ".xml",
    ".css", ".scss", ".less",
    ".po", ".pot", ".properties",
})


def _is_data_file(path: str) -> bool:
    """True if *path* is a data/doc/UI-string file (not source code)."""
    lowered = path.lower()
    if any(lowered.endswith(suf) for suf in _DATA_FILE_SUFFIXES):
        return True
    # i18n directories often hold .js/.ts files that are pure string maps.
    return "/i18n/" in lowered or lowered.startswith("i18n/")


def _name_in_base_file(
    repo_dir: Path,
    file_path: str,
    name: str,
    base_ref: str,
    timeout: int = 30,
) -> bool:
    """Did ``name`` already appear in ``file_path`` at ``base_ref``?

    True means the token is unrelated to the current rename — it pre-existed
    in this file before any patch was applied. False means the token is new
    in this file's view (so a hit in the working tree is a possible residue
    of the rename). Used to subtract baseline noise: a same-package
    file that just happens to define another symbol with the colliding
    name (e.g. ``type Scrobble struct`` in a response model, vs. a
    ``Client.Scrobble`` method that got unexported elsewhere) was already
    on disk at base, so the residue gate should NOT flag it.

    Errors (file did not exist at base_ref, git failure, etc.) are
    treated conservatively as ``False`` — when in doubt, keep the residue
    in the report so a real defect is never silently dropped.
    """
    rc, content = _run_git(
        ["git", "show", f"{base_ref}:{file_path}"],
        repo_dir,
        timeout=timeout,
    )
    if rc != 0:
        return False
    # Word-boundary match identical to the residue grep so a hit here means
    # the same kind of reference would also match the residue regex.
    return re.search(rf"\b{re.escape(name)}\b", content) is not None


def _run_git(cmd: list[str], repo_dir: Path, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _extract_rename_candidates_per_dir(diff_text: str) -> dict[str, set[str]]:
    """Return ``{dir_path: {removed_name, ...}}`` — for each directory the
    patch touched, the set of identifiers that disappeared from files in
    that directory.

    A "directory" here is the parent of the changed file, used as a proxy
    for the language's package/module scope (Go: package == directory;
    Python: same dir is usually the same module/subpackage; JS: a folder
    is usually a logical unit). Residue greps later restrict themselves
    to these directories so that an identifier that happens to also exist
    in an unrelated package is NOT flagged.

    Implementation parses ``diff --git a/<path> b/<path>`` headers, then
    accumulates ``-``-line / ``+``-line tokens per file; the per-file
    removed set is ``-`` minus ``+``. The result is unioned per directory.
    """
    by_file_minus: dict[str, set[str]] = {}
    by_file_plus: dict[str, set[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                current = parts[1].strip().replace("\\", "/")
            else:
                current = None
            continue
        if current is None or not line:
            continue
        if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            target = by_file_minus.setdefault(current, set())
        elif line.startswith("+"):
            target = by_file_plus.setdefault(current, set())
        else:
            continue
        for tok in _IDENT_RE.findall(line[1:]):
            if len(tok) < 4 or tok in _RENAME_BLACKLIST:
                continue
            target.add(tok)
    by_dir: dict[str, set[str]] = {}
    for file_path, removed in by_file_minus.items():
        truly_removed = removed - by_file_plus.get(file_path, set())
        if not truly_removed:
            continue
        dir_path = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        by_dir.setdefault(dir_path, set()).update(truly_removed)
    return by_dir


def check_rename_residue(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect old symbols left behind after a rename.

    Compares the working tree against ``base_commit`` (or HEAD if None),
    extracts ``(directory, removed_name)`` candidates from the diff, then
    greps each removed name **only inside the directory the rename
    happened in** (treated as a proxy for package scope). Hits in files
    that are NOT part of that directory's change set are flagged as
    residue.

    Restricting the grep to the rename's own package is what keeps benign
    cross-package collisions of common identifiers (Go ``http.Client``,
    JSON i18n strings, model field names) from drowning real residues in
    noise. A file in the same directory that the patch did not touch is
    still suspect — it's the same Go package, same module, same logical
    unit. Hits there are real residue.

    Returned BuildErrors are shaped to match ``build_verify.BuildError`` so
    they can be appended to the same ``new_errors`` list and feed into the
    existing repatch loop.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"

    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []
    removed_per_dir = _extract_rename_candidates_per_dir(diff_text)
    if not removed_per_dir:
        return []

    # Files modified by the patch — residues *inside* these are usually the
    # rename itself; we skip them so we only flag unupdated callers.
    rc, name_only = _run_git(
        ["git", "diff", ref, "--name-only"], repo_dir, timeout=timeout,
    )
    modified_files: set[str] = set()
    if rc == 0:
        for line in name_only.splitlines():
            line = line.strip().replace("\\", "/")
            if line:
                modified_files.add(line)

    residues: list[BuildError] = []
    seen_sigs: set[str] = set()
    for dir_path in sorted(removed_per_dir.keys()):
        # Limit the grep to this rename's own directory. ``git grep`` accepts
        # a pathspec at the end; an empty dir_path means repo root, in which
        # case we still scope to the root non-recursively-ish via "." (the
        # patched file lived at repo top, so siblings only — but in practice
        # most repos don't put renames at the very root).
        scope = dir_path if dir_path else "."
        for old_name in sorted(removed_per_dir[dir_path]):
            rc, hits = _run_git(
                [
                    "git", "grep", "-n", "--no-color",
                    "-E", rf"\b{re.escape(old_name)}\b",
                    "--", scope,
                ],
                repo_dir,
                timeout=timeout,
            )
            if rc != 0 or not hits.strip():
                continue
            for hit_line in hits.splitlines():
                # Format: ``path:line:content``
                parts = hit_line.split(":", 2)
                if len(parts) < 3:
                    continue
                file_path = parts[0].replace("\\", "/")
                try:
                    line_no = int(parts[1])
                except ValueError:
                    continue
                content = parts[2]
                if file_path in modified_files:
                    # The patch already touched this file; per-file completeness
                    # is the build gate's job. Residue gate only flags callers
                    # in files the patch never opened.
                    continue
                if _is_data_file(file_path):
                    # Data / doc / UI-string files — string collisions, not
                    # missed renames. Skip wholesale.
                    continue
                if _name_in_base_file(repo_dir, file_path, old_name, ref):
                    # The token already existed in this file at base_commit,
                    # so it is an unrelated symbol with the same spelling
                    # (e.g. a different ``type Scrobble struct`` in a
                    # response-model file when ``Client.Scrobble`` got
                    # unexported elsewhere). Not residue.
                    continue
                sig = f"{file_path}::{old_name}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                residues.append(BuildError(
                    file=file_path,
                    line=line_no,
                    message=(
                        f"rename residue: removed symbol '{old_name}' still "
                        f"referenced in unmodified file (line: "
                        f"{content.strip()[:120]})"
                    ),
                    raw=hit_line,
                ))

    return residues


def render_residue_for_feedback(residues: list[BuildError], limit: int = 30) -> str:
    """Render residue findings as a compact text block for planner feedback."""
    if not residues:
        return ""
    lines: list[str] = ["Rename-residue gate found unupdated old-symbol references:"]
    for err in residues[:limit]:
        loc = f"{err.file}:{err.line}" if err.line is not None else err.file
        lines.append(f"- {loc}: {err.message}")
    if len(residues) > limit:
        lines.append(f"- ... and {len(residues) - limit} more")
    return "\n".join(lines)
