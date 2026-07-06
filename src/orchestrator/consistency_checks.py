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


# ── Undefined-config-symbol gate (phase 24) ─────────────────────────────────

# Structured-config formats where a bare CamelCase value conventionally names a
# code symbol resolved at load time (qutebrowser ``configdata.yml`` ``type:
# Foo``; Django settings; DRF serializer registries; DI wiring). Narrower than
# ``_DATA_FILE_SUFFIXES`` on purpose: prose/markup (.md/.html/.css) hold
# CamelCase strings that are never symbol references.
_CONFIG_REF_SUFFIXES: frozenset[str] = frozenset(
    {".yml", ".yaml", ".json", ".jsonc", ".toml", ".ini", ".cfg"}
)

# A standalone CamelCase identifier with an internal lower→upper transition —
# the shape of a class/type name (VersionChangeFilter, ChangelogAfterUpgrade).
# The lookbehind/lookahead reject dotted paths so enum *values* written in
# config (``VersionChange.major``) are NOT mistaken for type references.
_CONFIG_TYPE_REF_RE = re.compile(
    r"(?<![.\w])([A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)(?![\w.])"
)

# Definition keywords across languages. A config-referenced symbol counts as
# "defined" if it appears after any of these (``class Foo``, ``type Foo``,
# ``func Foo`` …) or as a bare assignment target (``Foo = ...``). YAML/JSON use
# ``key: Value`` (colon, no space-after-keyword), so config lines never satisfy
# these patterns — a self-reference in the data file is not a definition.
_DEF_KEYWORDS = (
    "class", "def", "type", "interface", "struct", "enum",
    "func", "function", "trait", "object", "record",
)


def _added_config_type_refs(diff_text: str) -> dict[str, set[str]]:
    """Return ``{config_file: {CamelCaseSymbol, ...}}`` for symbols introduced
    by the patch into structured-config files.

    Only ``+`` lines in files with a ``_CONFIG_REF_SUFFIXES`` extension are
    scanned, so a symbol that already lived in the config at base_commit is
    never re-flagged — we only police references the patch itself added.
    """
    refs: dict[str, set[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1].strip().replace("\\", "/") if len(parts) == 2 else None
            continue
        if current is None:
            continue
        suffix = "." + current.rsplit(".", 1)[1].lower() if "." in current else ""
        if suffix not in _CONFIG_REF_SUFFIXES:
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for m in _CONFIG_TYPE_REF_RE.finditer(line[1:]):
            refs.setdefault(current, set()).add(m.group(1))
    return refs


def _symbol_defined_in_tree(repo_dir: Path, name: str, timeout: int) -> bool:
    """True if *name* is defined as a code symbol anywhere in the working tree.

    Greps the patched tree (not a git ref) for a definition keyword followed by
    the name, or a bare assignment ``Name = ...`` (Python/JS class-via-callable,
    enum aliasing). Conservative: any git error returns True so we never flag a
    symbol we merely failed to search for.
    """
    kw = "|".join(_DEF_KEYWORDS)
    pattern = rf"(^|\s)({kw})\s+{re.escape(name)}\b|(^|\s){re.escape(name)}\s*="
    rc, hits = _run_git(
        ["git", "grep", "-E", "-l", "--no-color", pattern],
        repo_dir,
        timeout=timeout,
    )
    if rc not in (0, 1):
        # rc 0 = matches found, 1 = no match; anything else is a git failure.
        return True
    return bool(hits.strip())


def check_undefined_config_symbol(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect config/data files that reference a code symbol never defined.

    Catches the late-binding failure mode the build gate's Python path misses:
    a structured-config file (``configdata.yml`` ``type: Foo``, Django/DRF
    registries, DI wiring) names a class/type that ``--collect-only`` does not
    resolve because the lookup happens at config-load / fixture time, not at
    import. Observed on issue 008: ``configdata.yml`` set ``type:
    VersionChangeFilter`` while the patch defined the class as
    ``ChangelogAfterUpgrade`` — every test errored at setup, yet
    ``pytest --collect-only`` reported ``ok``.

    The patch is diffed against ``base_commit`` (HEAD when None); only symbols
    the patch *added* to config files are checked. A symbol is "defined" if it
    appears after a definition keyword (or as an assignment target) anywhere in
    the patched tree. Undefined references are returned as ``BuildError`` so
    they ride the existing repatch feedback loop.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"

    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []
    refs_per_file = _added_config_type_refs(diff_text)
    if not refs_per_file:
        return []

    errors: list[BuildError] = []
    seen_sigs: set[str] = set()
    # One grep per distinct symbol, cached across files referencing it.
    defined_cache: dict[str, bool] = {}
    for config_file in sorted(refs_per_file.keys()):
        for name in sorted(refs_per_file[config_file]):
            if name not in defined_cache:
                defined_cache[name] = _symbol_defined_in_tree(repo_dir, name, timeout)
            if defined_cache[name]:
                continue
            sig = f"{config_file}::{name}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            errors.append(BuildError(
                file=config_file,
                line=None,
                message=(
                    f"undefined config symbol: '{name}' is referenced in "
                    f"{config_file} but no matching class/type/func definition "
                    f"exists in the patched tree. The config loader resolves "
                    f"this name at load time; an unresolved reference raises "
                    f"AttributeError before any test runs (collection passes, "
                    f"setup fails). Define '{name}' or correct the reference to "
                    f"the symbol that was actually added."
                ),
                raw=f"{config_file}: {name}",
            ))
    return errors


def render_undefined_config_symbol_for_feedback(
    errors: list[BuildError], limit: int = 30
) -> str:
    """Render undefined-config-symbol findings as a feedback text block."""
    if not errors:
        return ""
    lines: list[str] = [
        "Undefined-config-symbol gate found config references with no definition:"
    ]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 27 deterministic gates (all git-only, fold into deterministic_errors)
#
# Five additional PatchVerifying gates motivated by the first 15-case eval:
#   A. check_contract_drift            — empty-value return drift (issue 001)
#   B. check_parallel_impl_consistency — N sibling impls, unaligned guards (001)
#   C. check_removed_symbol_test_refs  — deleted symbol still used by tests (009)
#   D. check_go_unexport_consistency   — case-flipped type, exported leftovers (010)
#   E. check_config_entry_shape        — config entry shape vs siblings (008)
#
# All run even when the build toolchain is unavailable (JS repos, missing go),
# which is exactly the path where they are the only line of defense.
# ═══════════════════════════════════════════════════════════════════════════


def _iter_diff_file_hunks(diff_text: str):
    """Yield ``(file_path, hunks)`` where each hunk is a list of diff lines
    (including their leading ``-``/``+``/`` `` markers, excluding headers)."""
    current: str | None = None
    hunks: list[list[str]] = []
    hunk: list[str] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                if hunk:
                    hunks.append(hunk)
                yield current, hunks
            parts = line.split(" b/", 1)
            current = parts[1].strip().replace("\\", "/") if len(parts) == 2 else None
            hunks, hunk = [], None
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            if hunk:
                hunks.append(hunk)
            hunk = []
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if hunk is not None:
            hunk.append(line)
    if current is not None:
        if hunk:
            hunks.append(hunk)
        yield current, hunks


# ── Gate A: contract drift (conservative empty-value return drift) ─────────

# Null-class literals: "absence of a value". Empty-class literals: "a present
# but empty/zero value". Flipping between the two classes on an *existing*
# return path changes the provider's observable contract for every caller.
_NULLCLASS_RE = (
    r"(?<![\w.])(?:null|nil|None|undefined)(?![\w.])"
)
_EMPTYCLASS_RE = (
    r"(?<![\w.])(?:0(?:\.0)?|false|False)(?![\w.])"
    r"|(?<!\\)(?:''|\"\")"
    r"|\[\s*\]|\{\s*\}"
)
_DRIFT_LITERAL_RE = re.compile(f"(?:{_NULLCLASS_RE})|(?:{_EMPTYCLASS_RE})")
_NULLCLASS_ONLY_RE = re.compile(_NULLCLASS_RE)


def _literal_class(token: str) -> str:
    return "null" if _NULLCLASS_ONLY_RE.fullmatch(token) else "empty"


def _drift_skeleton(line: str) -> tuple[str, list[str]]:
    """Replace every null/empty-class literal with a placeholder.

    Returns ``(skeleton, slot_classes)`` where slot_classes[i] is the class
    ("null"/"empty") of the i-th literal. Two lines with equal skeletons are
    the same statement modulo those literals.
    """
    slots: list[str] = []

    def _sub(m: re.Match) -> str:
        slots.append(_literal_class(m.group(0)))
        return "\x00"

    skeleton = _DRIFT_LITERAL_RE.sub(_sub, " ".join(line.split()))
    return skeleton, slots


def check_contract_drift(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect empty-value semantic drift on existing return paths.

    Conservative by design: only fires when a ``-`` line containing ``return``
    and a null-class literal (null/nil/None/undefined) is paired — same hunk,
    identical statement skeleton — with a ``+`` line where that literal slot
    flipped to the empty class (0/''/[]/{}/false), or vice versa. This is the
    issue-001 failure shape: ``return pending ? db.pttl(..) : null`` rewritten
    to ``: 0`` on a branch no requirement named, silently changing the
    provider contract for every caller. Legitimate rewrites (new functions,
    restructured statements) produce different skeletons and never match.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    errors: list[BuildError] = []
    seen_sigs: set[str] = set()
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if _is_data_file(file_path):
            continue
        for hunk in hunks:
            minus = [l[1:] for l in hunk if l.startswith("-")]
            plus = [l[1:] for l in hunk if l.startswith("+")]
            if not minus or not plus:
                continue
            plus_skeletons: dict[str, list[tuple[list[str], str]]] = {}
            for p in plus:
                if "return" not in p:
                    continue
                sk, slots = _drift_skeleton(p)
                if slots:
                    plus_skeletons.setdefault(sk, []).append((slots, p))
            if not plus_skeletons:
                continue
            for m in minus:
                if "return" not in m:
                    continue
                sk, m_slots = _drift_skeleton(m)
                if not m_slots or sk not in plus_skeletons:
                    continue
                for p_slots, p_line in plus_skeletons[sk]:
                    if len(p_slots) != len(m_slots):
                        continue
                    flipped = [
                        i for i in range(len(m_slots)) if m_slots[i] != p_slots[i]
                    ]
                    if not flipped:
                        continue
                    sig = f"{file_path}::{m.strip()[:80]}"
                    if sig in seen_sigs:
                        break
                    seen_sigs.add(sig)
                    errors.append(BuildError(
                        file=file_path,
                        line=None,
                        message=(
                            "contract drift: an existing return path changed its "
                            "empty-value semantics "
                            f"(base: `{m.strip()[:100]}` → patched: "
                            f"`{p_line.strip()[:100]}`). Flipping between "
                            "null/None/nil and 0/''/[] on a branch the "
                            "requirements did not name changes the provider's "
                            "observable contract for every caller. Preserve the "
                            "base return value verbatim and normalize at the "
                            "consumer side instead — unless a requirement "
                            "explicitly demands this exact change."
                        ),
                        raw=f"{m.strip()} -> {p_line.strip()}",
                    ))
                    break
    return errors


def render_contract_drift_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Contract-drift gate flagged empty-value semantic changes on existing return paths:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


# ── Gate B: parallel-implementation consistency ─────────────────────────────

# Added-function-definition patterns per language family. Each must expose the
# function name as group 1.
_ADDED_DEF_PATTERNS: tuple[re.Pattern, ...] = (
    # JS/TS: module.mget = async function (keys) { … } / const x = function…
    re.compile(r"^(?:[\w$]+\.)*([\w$]+)\s*=\s*(?:async\s+)?function\b"),
    # JS/TS: function name(…)  /  async function name(…)
    re.compile(r"^(?:async\s+)?function\s+([\w$]+)\s*\("),
    # JS/TS object literal: name: async function (…)
    re.compile(r"^([\w$]+)\s*:\s*(?:async\s+)?function\b"),
    # Python: def name(…)
    re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\("),
    # Go: func (r *T) name(…)  /  func name(…)
    re.compile(r"^func\s*(?:\([^)]*\)\s*)?(\w+)\s*\("),
)

_GUARD_KEYWORD_BLACKLIST = frozenset({
    "if", "for", "while", "switch", "catch", "return", "else",
})

# An early-return guard line: an ``if`` testing negation/emptiness.
_GUARD_LINE_RE = re.compile(
    r"^if\b.*(?:!|\bnot\b|\blen\(|\.length\b|==\s*0|===\s*0|\bis\s+None\b|==\s*nil\b)"
)


def _extract_added_functions(diff_text: str) -> dict[str, list[tuple[str, list[str]]]]:
    """Return ``{func_name: [(file, body_lines), ...]}`` for functions whose
    definition line was *added* by the patch. body_lines are the following
    added lines in the same hunk (up to 8)."""
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if _is_data_file(file_path):
            continue
        for hunk in hunks:
            added = [l[1:] for l in hunk if l.startswith("+")]
            i = 0
            while i < len(added):
                stripped = added[i].strip()
                name = None
                for pat in _ADDED_DEF_PATTERNS:
                    m = pat.match(stripped)
                    if m and m.group(1) not in _GUARD_KEYWORD_BLACKLIST:
                        name = m.group(1)
                        break
                if name is None:
                    i += 1
                    continue
                body: list[str] = []
                j = i + 1
                while j < len(added) and len(body) < 8:
                    b = added[j].strip()
                    if any(p.match(b) for p in _ADDED_DEF_PATTERNS):
                        break
                    body.append(b)
                    j += 1
                out.setdefault(name, []).append((file_path, body))
                i = j
    return out


def _has_leading_guard(body: list[str]) -> bool:
    """True when one of the first 4 body lines is an emptiness/negation guard
    with a return nearby (same line or within the next 2 lines)."""
    for idx, line in enumerate(body[:4]):
        if not _GUARD_LINE_RE.match(line):
            continue
        window = " ".join(body[idx:idx + 3])
        if "return" in window:
            return True
    return False


def check_parallel_impl_consistency(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect a guard-shape minority among N parallel same-named additions.

    When the patch adds the same-named function/method to ≥2 files (the
    multi-backend adapter pattern: redis/mongo/postgres ``mget``, N agents
    implementing one new interface), their entry guards must align. If a
    clear majority opens with an emptiness/negation early-return guard and a
    minority does not, the minority is flagged (issue-001 shape: redis
    ``mget`` missing the empty-array guard its mongo/postgres siblings have).
    Fires only with ≥2 guarded implementations and guarded > unguarded.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    groups = _extract_added_functions(diff_text)
    errors: list[BuildError] = []
    for name, impls in sorted(groups.items()):
        files = {f for f, _ in impls}
        if len(files) < 2:
            continue
        guarded = [(f, b) for f, b in impls if _has_leading_guard(b)]
        unguarded = [(f, b) for f, b in impls if not _has_leading_guard(b)]
        if len(guarded) < 2 or len(guarded) <= len(unguarded):
            continue
        guarded_files = sorted({f for f, _ in guarded})
        for f, _body in unguarded:
            errors.append(BuildError(
                file=f,
                line=None,
                message=(
                    f"parallel-implementation inconsistency: '{name}' was added "
                    f"to {len(files)} files; the implementations in "
                    f"{', '.join(guarded_files)} open with an empty/invalid-input "
                    f"early-return guard, but the one in {f} does not. Sibling "
                    "implementations of the same new interface must handle the "
                    "same boundary inputs — add the matching guard (or align "
                    "all siblings deliberately)."
                ),
                raw=f"{f}: {name} lacks the guard present in {len(guarded)} siblings",
            ))
    return errors


def render_parallel_impl_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Parallel-implementation gate found unaligned sibling implementations:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


# ── Gate C: removed symbols still referenced by test files ───────────────────

# Definition-shaped lines. Capturing group 1 = the symbol name. These are
# deliberately narrower than the rename-candidate token extraction: only a
# line that *defines* something counts, so usage churn never produces a
# candidate.
_REMOVED_DEF_PATTERNS: tuple[re.Pattern, ...] = (
    # Go: func (s *T) Name(  /  func Name(
    re.compile(r"^func\s*(?:\([^)]*\)\s*)?(\w+)\s*\("),
    # Go struct field / var-block entry: ``name Type`` (no ':', no '=')
    re.compile(r"^(\w+)\s+\*?[\w.\[\]{}]+(?:\s*//.*)?$"),
    # Python: def name( / class Name
    re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\("),
    re.compile(r"^class\s+(\w+)\b"),
    # Python attribute definition: self.name =
    re.compile(r"^self\.(\w+)\s*="),
    # JS/TS: function name( / name = function / name: function
    re.compile(r"^(?:async\s+)?function\s+([\w$]+)\s*\("),
    re.compile(r"^(?:[\w$]+\.)*([\w$]+)\s*=\s*(?:async\s+)?function\b"),
    re.compile(r"^([\w$]+)\s*:\s*(?:async\s+)?function\b"),
)

_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__)/|_test\.\w+$|\.test\.\w+$|\.spec\.\w+$|(^|/)test_[^/]+$|_test_[^/]*\.py$"
)


def is_test_file(path: str) -> bool:
    """True when *path* looks like a test file in any supported ecosystem."""
    return bool(_TEST_FILE_RE.search(path.replace("\\", "/")))


def revert_test_file_edits(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[str]:
    """Revert every test-file change the patch made; return reverted paths.

    Under the SWE-bench evaluation protocol the evaluator applies its own
    test patch on top of the model patch — test files are the evaluator's
    property. A model edit to a test file is at best ignored and at worst
    collides with the injected gold tests (issue 002: a model-authored
    ``TestHideQtWarning`` shadowed the gold class of the same name and failed
    4 tests even though the production change was correct). Tracked test
    files modified or deleted by the patch are checked out back to
    ``base_commit``; untracked new test files are removed.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    reverted: list[str] = []

    rc, name_only = _run_git(
        ["git", "diff", ref, "--name-only"], repo_dir, timeout=timeout
    )
    if rc == 0:
        tracked_tests = [
            p.strip().replace("\\", "/")
            for p in name_only.splitlines()
            if p.strip() and is_test_file(p.strip())
        ]
        for path in tracked_tests:
            rc2, _ = _run_git(
                ["git", "checkout", ref, "--", path], repo_dir, timeout=timeout
            )
            if rc2 == 0:
                reverted.append(path)

    rc, untracked = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard"],
        repo_dir,
        timeout=timeout,
    )
    if rc == 0:
        for path in untracked.splitlines():
            path = path.strip().replace("\\", "/")
            if not path or not is_test_file(path):
                continue
            try:
                (repo_dir / path).unlink()
                reverted.append(path)
            except OSError:
                pass

    return sorted(set(reverted))


def _extract_removed_defs(diff_text: str) -> tuple[dict[str, set[str]], set[str]]:
    """Return ``({file: {removed_def_name}}, {added_def_name_anywhere})``."""
    removed_per_file: dict[str, set[str]] = {}
    added_anywhere: set[str] = set()
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if _is_data_file(file_path) or is_test_file(file_path):
            continue
        for hunk in hunks:
            for line in hunk:
                if line.startswith("-"):
                    target, text = removed_per_file.setdefault(file_path, set()), line[1:].strip()
                elif line.startswith("+"):
                    target, text = added_anywhere, line[1:].strip()
                else:
                    continue
                for pat in _REMOVED_DEF_PATTERNS:
                    m = pat.match(text)
                    if not m:
                        continue
                    name = m.group(1)
                    if len(name) >= 3 and name not in _RENAME_BLACKLIST:
                        target.add(name)
                    break
    return removed_per_file, added_anywhere


def _candidate_test_files(repo_dir: Path, src_file: str) -> list[str]:
    """Test files plausibly bound to *src_file*: same directory, or a name
    derived from the source stem anywhere in the repo."""
    rc, out = _run_git(["git", "ls-files"], repo_dir, timeout=60)
    if rc != 0:
        return []
    src_dir = src_file.rsplit("/", 1)[0] if "/" in src_file else ""
    stem = src_file.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem_lower = stem.lower()
    candidates: list[str] = []
    for path in out.splitlines():
        path = path.strip().replace("\\", "/")
        if not path or not is_test_file(path):
            continue
        base = path.rsplit("/", 1)[-1].lower()
        in_same_dir = ("/" in path and path.rsplit("/", 1)[0] == src_dir) or (
            "/" not in path and src_dir == ""
        )
        name_bound = (
            base.startswith(f"test_{stem_lower}")
            or base.startswith(f"{stem_lower}_test")
            or base.startswith(f"{stem_lower}.test")
            or base.startswith(f"{stem_lower}.spec")
        )
        if in_same_dir or name_bound:
            candidates.append(path)
    return candidates


def check_removed_symbol_test_refs(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect symbols the patch deleted that test files still reference.

    The rename-residue gate intentionally suppresses hits that pre-existed at
    base (``_name_in_base_file``) — correct for renames, but for *deletions*
    the pre-existing reference is exactly the defect: evaluation compiles
    same-package ``_test.go`` files and imports ``test_*.py`` modules against
    the patched production code, so a field/method/function whose definition
    the patch removed while a test still names it fails the whole suite
    (issue-009 shape: ``Forwarder.cfg`` / ``clusterSession.forwarder`` deleted,
    ``forwarder_test.go`` still constructs them).

    Precision comes from definition-shaped extraction (only ``-`` lines that
    *define* a symbol count), name-bound test-file scoping, and skipping any
    name the patch re-defines elsewhere.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    removed_per_file, added_anywhere = _extract_removed_defs(diff_text)
    if not removed_per_file:
        return []

    errors: list[BuildError] = []
    seen_sigs: set[str] = set()
    test_file_cache: dict[str, list[str]] = {}
    for src_file in sorted(removed_per_file.keys()):
        deleted = removed_per_file[src_file] - added_anywhere
        if not deleted:
            continue
        if src_file not in test_file_cache:
            test_file_cache[src_file] = _candidate_test_files(repo_dir, src_file)
        for test_path in test_file_cache[src_file]:
            try:
                content = (repo_dir / test_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            for name in sorted(deleted):
                pattern = re.compile(
                    rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
                )
                m = pattern.search(content)
                if not m:
                    continue
                line_no = content.count("\n", 0, m.start()) + 1
                sig = f"{test_path}::{name}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                errors.append(BuildError(
                    file=src_file,
                    line=None,
                    message=(
                        f"removed symbol still referenced by tests: the patch "
                        f"deleted the definition of '{name}' from {src_file}, "
                        f"but {test_path}:{line_no} still references it. Test "
                        "files are compiled/imported against the patched "
                        "production code at evaluation time, so this fails the "
                        "whole suite. Unless a requirement explicitly demands "
                        f"removing '{name}', restore it (do NOT edit the test "
                        "file — tests are owned by the evaluator)."
                    ),
                    raw=f"{test_path}:{line_no}: {name}",
                ))
    return errors


def render_removed_symbol_test_refs_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Removed-symbol gate found deleted symbols that test files still reference:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


# ── Gate D: Go unexport consistency ─────────────────────────────────────────

_GO_TYPE_DEF_RE = re.compile(r"^type\s+(\w+)\s+(?:struct|interface)\b")


def check_go_unexport_consistency(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """After a Go type case-flip (``Client`` → ``client``), every method on
    the lowercased receiver and the ``New<Type>`` constructor must flip too.

    Go decides exportedness per identifier: ``func (c *client) ValidateToken``
    is still exported even though the receiver type is not. A patch that
    lowercases only the type leaves the unexport half-done (issue-010 shape:
    listenbrainz/spotify kept ``NewClient`` and exported methods). This is a
    pure syntax fact — an uppercase method on a freshly-lowercased type in an
    unexport refactor is always a defect — so the gate greps the patched
    working tree of the type's own package and flags every leftover.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    # (dir, OldName, newname) for every case-flipped type definition.
    flips: list[tuple[str, str, str]] = []
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if not file_path.endswith(".go") or file_path.endswith("_test.go"):
            continue
        removed_types: set[str] = set()
        added_types: set[str] = set()
        for hunk in hunks:
            for line in hunk:
                if line.startswith("-"):
                    m = _GO_TYPE_DEF_RE.match(line[1:].strip())
                    if m:
                        removed_types.add(m.group(1))
                elif line.startswith("+"):
                    m = _GO_TYPE_DEF_RE.match(line[1:].strip())
                    if m:
                        added_types.add(m.group(1))
        dir_path = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        for old in removed_types:
            if not old[:1].isupper():
                continue
            flipped = old[:1].lower() + old[1:]
            if flipped in added_types:
                flips.append((dir_path, old, flipped))

    if not flips:
        return []

    errors: list[BuildError] = []
    seen_sigs: set[str] = set()
    for dir_path, old_name, new_name in sorted(set(flips)):
        scope = dir_path if dir_path else "."
        rc, out = _run_git(
            ["git", "ls-files", "--", f"{scope}/*.go" if dir_path else "*.go"],
            repo_dir,
            timeout=timeout,
        )
        if rc != 0:
            continue
        method_re = re.compile(
            rf"func\s*\(\s*\w+\s+\*?{re.escape(new_name)}\s*\)\s*([A-Z]\w*)\s*\("
        )
        ctor_re = re.compile(rf"func\s+(New{re.escape(old_name)})\s*\(")
        for go_file in out.splitlines():
            go_file = go_file.strip().replace("\\", "/")
            if not go_file or go_file.endswith("_test.go"):
                continue
            try:
                content = (repo_dir / go_file).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            leftovers: list[str] = []
            leftovers.extend(m.group(1) for m in method_re.finditer(content))
            leftovers.extend(m.group(1) for m in ctor_re.finditer(content))
            for ident in leftovers:
                sig = f"{go_file}::{ident}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                lowered = ident[:1].lower() + ident[1:] if not ident.startswith("New") \
                    else f"new{old_name}"
                errors.append(BuildError(
                    file=go_file,
                    line=None,
                    message=(
                        f"incomplete Go unexport: type '{old_name}' was "
                        f"lowercased to '{new_name}', but '{ident}' in "
                        f"{go_file} is still exported. Go exportedness is "
                        "per-identifier — lowercasing the type does NOT "
                        f"unexport its methods or constructor. Rename "
                        f"'{ident}' to '{lowered}' and update every "
                        "same-package call site."
                    ),
                    raw=f"{go_file}: {ident} (receiver {new_name})",
                ))
    return errors


def render_go_unexport_for_feedback(
    errors: list[BuildError], limit: int = 30
) -> str:
    if not errors:
        return ""
    lines = ["Go-unexport gate found exported leftovers on case-flipped types:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


# ── Gate E: config entry shape conformance ──────────────────────────────────

def _entry_shape(entry) -> tuple:
    """Shape of one list entry: scalars by kind, mappings by key count."""
    if isinstance(entry, dict):
        return ("map", len(entry))
    if isinstance(entry, list):
        return ("list",)
    return ("scalar",)


def _collect_keyed_lists(node, out: dict[str, list]) -> None:
    """Accumulate ``{mapping_key: [list entries across the whole doc]}``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list):
                out.setdefault(str(key), []).extend(value)
            _collect_keyed_lists(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_keyed_lists(item, out)


def check_config_entry_shape(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """New entries in a YAML config must match the shape of their siblings.

    Structured config files are parsed by code-side loaders with assertions
    about entry shape (issue-008: qutebrowser ``ValidValues.__init__`` asserts
    every ``valid_values`` mapping entry has exactly one key, while the patch
    wrote ``{name, desc}`` two-key entries — every test errored at setup, and
    no build gate loads configdata). The convention is keyed by the mapping
    key name: all lists that appear as the value of e.g. ``valid_values``
    anywhere in the *base* document define the allowed entry shapes; an entry
    the patch adds under the same key with a shape matching NO base sibling is
    flagged. Files with no base version, unparsable YAML, or keys never seen
    at base are skipped (soft pass — this gate only enforces conventions that
    demonstrably exist).
    """
    try:
        import yaml
    except ImportError:
        return []

    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, name_only = _run_git(
        ["git", "diff", ref, "--name-only"], repo_dir, timeout=timeout
    )
    if rc != 0:
        return []
    yaml_files = [
        p.strip().replace("\\", "/")
        for p in name_only.splitlines()
        if p.strip().lower().endswith((".yml", ".yaml"))
    ]
    if not yaml_files:
        return []

    def _load_all(text: str):
        try:
            return [d for d in yaml.safe_load_all(text) if d is not None]
        except yaml.YAMLError:
            return None

    errors: list[BuildError] = []
    for cfg_file in yaml_files:
        rc, base_text = _run_git(
            ["git", "show", f"{ref}:{cfg_file}"], repo_dir, timeout=timeout
        )
        if rc != 0:
            continue  # new file at patch time — no base convention to enforce
        try:
            new_text = (repo_dir / cfg_file).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        base_docs = _load_all(base_text)
        new_docs = _load_all(new_text)
        if base_docs is None or new_docs is None:
            continue

        base_lists: dict[str, list] = {}
        new_lists: dict[str, list] = {}
        for d in base_docs:
            _collect_keyed_lists(d, base_lists)
        for d in new_docs:
            _collect_keyed_lists(d, new_lists)

        for key, new_entries in sorted(new_lists.items()):
            base_entries = base_lists.get(key)
            if not base_entries:
                continue
            base_shapes = {_entry_shape(e) for e in base_entries}
            for entry in new_entries:
                if not isinstance(entry, dict):
                    continue  # scalar additions are low-risk; mappings carry shape
                if any(entry == b for b in base_entries):
                    continue  # pre-existing entry, not added by the patch
                shape = _entry_shape(entry)
                if shape in base_shapes:
                    continue
                shape_desc = ", ".join(
                    f"{s[0]}({s[1]} keys)" if s[0] == "map" else s[0]
                    for s in sorted(base_shapes)
                )
                errors.append(BuildError(
                    file=cfg_file,
                    line=None,
                    message=(
                        f"config entry shape mismatch: a new entry under "
                        f"'{key}' has keys {sorted(entry.keys())} "
                        f"({len(entry)} keys), but every existing '{key}' "
                        f"entry in this file at base has shape: {shape_desc}. "
                        "Config loaders assert entry shape at load time — "
                        "copy the exact shape of the existing sibling entries "
                        "instead of inventing field names."
                    ),
                    raw=f"{cfg_file}: {key}: {sorted(entry.keys())}",
                ))
    return errors


def render_config_entry_shape_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Config-entry-shape gate found entries that break the file's own conventions:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)
