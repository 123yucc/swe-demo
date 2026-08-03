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

import ast
import re
import subprocess
from collections import defaultdict
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
_ANCHOR_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def _structured_missing_element_path(text: str) -> str | None:
    match = re.search(r"\bPath\s*:\s*([^\s]+)", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip().strip("`'\".,;()") or None


def _structured_missing_element_symbol(text: str) -> str | None:
    explicit_name = re.search(
        r"\bName\s*:\s*`?\s*([A-Za-z_][A-Za-z0-9_]*)",
        text or "",
        flags=re.IGNORECASE,
    )
    if explicit_name:
        return explicit_name.group(1)
    structured_name = re.search(
        r"\bType\s*:\s*[A-Za-z_][A-Za-z0-9_]*\s+Name\s*:\s*"
        r"`?\s*([A-Za-z_][A-Za-z0-9_]*)",
        text or "",
        flags=re.IGNORECASE,
    )
    if structured_name:
        return structured_name.group(1)
    label = re.search(
        r"\b(?:Type|Struct|Interface|Class|Function|Method|Func|Const|Var|Field)\s*:\s*"
        r"`?\s*([A-Za-z_][A-Za-z0-9_]*)",
        text or "",
    )
    if label:
        return label.group(1)
    match = re.search(r"`([^`]+)`", text or "")
    if not match:
        return None
    ident = _ANCHOR_IDENT_RE.match(match.group(1).strip())
    return ident.group(0) if ident else None


def _future_anchor_allowances(
    evidence: EvidenceCards,
) -> tuple[set[str], dict[str, set[str]]]:
    """Return future file/symbol endpoints that analysis may reference."""
    future_paths: set[str] = set()
    future_symbols: dict[str, set[str]] = defaultdict(set)

    def _register(path: str | None, symbol: str | None = None) -> None:
        if not path:
            return
        norm = path.strip()
        if not norm:
            return
        future_paths.add(norm)
        if symbol:
            future_symbols[norm].add(symbol.strip())

    for req in [*evidence.requirements, *evidence.requirement_status]:
        verdict = getattr(req, "verdict", "")
        origin = getattr(req, "origin", "")
        contract_kind = getattr(req, "contract_kind", "")
        is_future = verdict in {"TO_BE_MISSING", "TO_BE_PARTIAL"}
        if not is_future and not (
            origin == "new_interfaces" and contract_kind == "interface"
        ):
            continue
        paths = list(getattr(req, "explicit_paths", []) or [])
        symbols = list(getattr(req, "explicit_symbols", []) or [])
        if symbols:
            for path in paths:
                for symbol in symbols:
                    _register(path, symbol)
        else:
            for path in paths:
                _register(path)

    for entry in evidence.constraint.missing_elements_to_implement:
        _register(
            _structured_missing_element_path(entry),
            _structured_missing_element_symbol(entry),
        )

    return future_paths, future_symbols


def _is_future_endpoint(
    endpoint: AnchorEndpoint,
    future_paths: set[str],
    future_symbols: dict[str, set[str]],
) -> bool:
    if endpoint.path not in future_paths:
        return False
    if endpoint.kind == "line":
        return True
    known_symbols = future_symbols.get(endpoint.path)
    return not known_symbols or endpoint.symbol_name in known_symbols


def _verify_endpoint(
    endpoint: AnchorEndpoint,
    repo_dir: Path,
    future_paths: set[str],
    future_symbols: dict[str, set[str]],
) -> str | None:
    """Return failure reason string, or None if endpoint resolves."""
    file_path = repo_dir / endpoint.path
    future_endpoint = _is_future_endpoint(endpoint, future_paths, future_symbols)
    if not file_path.is_file():
        if future_endpoint:
            return None
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
        if future_endpoint:
            return None
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
    future_paths, future_symbols = _future_anchor_allowances(evidence)

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
        lhs_err = _verify_endpoint(parsed.lhs, repo_dir, future_paths, future_symbols)
        rhs_err = _verify_endpoint(parsed.rhs, repo_dir, future_paths, future_symbols)
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

_PY_TOP_LEVEL_ALIAS_DEF_RE = re.compile(
    r"^([A-Za-z_]\w*)\s*=\s*(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:\s*(?:#.*)?)?$"
)
_PY_TOP_LEVEL_FROM_IMPORT_RE = re.compile(
    r"^from\s+[A-Za-z_][\w.]*\s+import\s+(.+)$"
)
_PY_TOP_LEVEL_IMPORT_RE = re.compile(r"^import\s+(.+)$")


def _python_top_level_import_defined_names(text: str) -> set[str]:
    """Names bound by a one-line top-level Python import statement.

    Compatibility shims often restore an old module's import surface as
    ``from new.owner import OldAPI`` rather than ``OldAPI = new.owner.OldAPI``.
    From a caller/test perspective both forms define ``OldAPI`` in the old
    module, so the removed-symbol gate must count both as restored symbols.
    """
    stripped = text.split("#", 1)[0].strip()
    if not stripped:
        return set()
    match = _PY_TOP_LEVEL_FROM_IMPORT_RE.match(stripped)
    if match:
        names: set[str] = set()
        imports = match.group(1).strip()
        if imports.startswith("(") and imports.endswith(")"):
            imports = imports[1:-1]
        for part in imports.split(","):
            item = part.strip()
            if not item or item == "*":
                continue
            bits = re.split(r"\s+as\s+", item, maxsplit=1)
            bound = bits[1].strip() if len(bits) == 2 else bits[0].strip()
            if re.match(r"^[A-Za-z_]\w*$", bound):
                names.add(bound)
        return names

    match = _PY_TOP_LEVEL_IMPORT_RE.match(stripped)
    if not match:
        return set()
    names = set()
    for part in match.group(1).split(","):
        item = part.strip()
        if not item:
            continue
        bits = re.split(r"\s+as\s+", item, maxsplit=1)
        if len(bits) == 2:
            bound = bits[1].strip()
        else:
            # ``import pkg.mod`` binds ``pkg`` at module top level.
            bound = bits[0].split(".", 1)[0].strip()
        if re.match(r"^[A-Za-z_]\w*$", bound):
            names.add(bound)
    return names

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


def _extract_removed_defs(
    diff_text: str,
) -> tuple[dict[str, set[str]], set[str], dict[str, set[str]]]:
    """Return removed/added definition names from a unified diff."""
    removed_per_file: dict[str, set[str]] = {}
    added_anywhere: set[str] = set()
    added_per_file: dict[str, set[str]] = {}
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if _is_data_file(file_path) or is_test_file(file_path):
            continue
        for hunk in hunks:
            for line in hunk:
                if line.startswith("-"):
                    target, raw_text = removed_per_file.setdefault(file_path, set()), line[1:]
                elif line.startswith("+"):
                    target, raw_text = added_anywhere, line[1:]
                else:
                    continue
                text = raw_text.strip()
                if file_path.endswith(".py") and (
                    text.startswith("global ") or text.startswith("nonlocal ")
                ):
                    # Python scope declarations such as ``global data_provider``
                    # are not production symbol definitions.  The Go struct
                    # field pattern below otherwise sees "global data_provider"
                    # as a removed symbol named "global" and sends static
                    # repair after a nonexistent compatibility target.
                    continue
                if (
                    line.startswith("+")
                    and file_path.endswith(".py")
                    and raw_text == raw_text.lstrip()
                ):
                    # Python re-export/compatibility shims often restore an
                    # importable symbol with a top-level alias rather than a
                    # new class/function definition, e.g.
                    # ``OldAPI = new_module.OldAPI``.  Tests importing
                    # ``OldAPI`` from the old module will still work, so the
                    # removed-symbol gate must count this as a restored symbol.
                    alias = _PY_TOP_LEVEL_ALIAS_DEF_RE.match(text)
                    if alias:
                        name = alias.group(1)
                        if len(name) >= 3 and name not in _RENAME_BLACKLIST:
                            target.add(name)
                            added_per_file.setdefault(file_path, set()).add(name)
                        continue
                    for name in _python_top_level_import_defined_names(text):
                        if len(name) >= 3 and name not in _RENAME_BLACKLIST:
                            target.add(name)
                            added_per_file.setdefault(file_path, set()).add(name)
                    if text.startswith(("from ", "import ")):
                        continue
                for pat in _REMOVED_DEF_PATTERNS:
                    m = pat.match(text)
                    if not m:
                        continue
                    name = m.group(1)
                    if len(name) >= 3 and name not in _RENAME_BLACKLIST:
                        target.add(name)
                        if line.startswith("+"):
                            added_per_file.setdefault(file_path, set()).add(name)
                    break
    return removed_per_file, added_anywhere, added_per_file


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

    removed_per_file, added_anywhere, added_per_file = _extract_removed_defs(diff_text)
    if not removed_per_file:
        return []

    errors: list[BuildError] = []
    seen_sigs: set[str] = set()
    test_file_cache: dict[str, list[str]] = {}
    for src_file in sorted(removed_per_file.keys()):
        deleted = removed_per_file[src_file] - added_anywhere
        if not deleted:
            continue
        added_here = added_per_file.get(src_file, set())
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
                if name[:1].isupper():
                    lowered = name[:1].lower() + name[1:]
                    # A same-file exported->unexported case-flip is often an
                    # intentional transport/internal-surface refactor. Base
                    # tests may still use the old exported spelling, but Stage2
                    # must not force the patch to preserve both names when the
                    # patch already introduced the unexported counterpart.
                    if lowered in added_here:
                        continue
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
                        f"deleted a production surface/member named '{name}' "
                        f"from {src_file}, "
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


# ── Gate F: Python helper protocol misuse ───────────────────────────────────

_PY_CLASS_ITERATION_CALL_RE = re.compile(
    r"\bfor\s+[_A-Za-z][_A-Za-z0-9]*\s+in\s+([A-Z][_A-Za-z0-9]*)\s*\("
)
_PY_CLASS_CALL_RE = re.compile(r"\b([A-Z][_A-Za-z0-9]*)\s*\((.*)\)")
_PY_CLASS_INLINE_METHOD_RE = re.compile(
    r"\b([A-Z][_A-Za-z0-9]*)\s*\([^()]*\)\s*\.\s*([A-Za-z_][_A-Za-z0-9]*)\s*\("
)
_PY_CONFIG_SUBSCRIPT_RE = re.compile(
    r"\b(?:config|config\.runtime_config)\s*\[\s*(['\"])([^'\"]+)\1\s*\]"
)
_PY_CONFIG_KEYERROR_HINT_RE = re.compile(
    r"\b(config|configured|configuration|setting|settings|section)\b",
    re.IGNORECASE,
)
_PUBLIC_DUNDER_METHODS = frozenset({
    "__add__",
    "__iadd__",
    "__sub__",
    "__isub__",
    "__mul__",
    "__imul__",
    "__or__",
    "__ior__",
    "__and__",
    "__iand__",
    "__iter__",
    "__next__",
    "__getitem__",
    "__setitem__",
    "__len__",
    "__bool__",
    "__eq__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__contains__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
})


def _python_class_has_iteration_protocol(source: str, class_name: str) -> bool | None:
    """Return whether class_name defines Python iteration protocol, or None if absent."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            return bool(methods & {"__iter__", "__next__", "__getitem__"})
    return None


def _repo_class_is_known_noniterable(
    repo_dir: Path,
    class_name: str,
    *,
    timeout: int,
) -> bool:
    """Conservatively decide whether a repo-defined class is not iterable.

    If no class definition is found, return False so external library classes are
    not flagged. If any matching definition exposes a Python iteration protocol,
    return False. Only flag when all found repo definitions lack iteration.
    """
    rc, hits = _run_git(
        [
            "git", "grep", "-n", "--no-color",
            "-E", rf"^class[[:space:]]+{re.escape(class_name)}\b",
            "--", "*.py",
        ],
        repo_dir,
        timeout=timeout,
    )
    if rc != 0 or not hits.strip():
        return False
    found = False
    for hit in hits.splitlines():
        path, _, _rest = hit.partition(":")
        if not path:
            continue
        file_path = repo_dir / path
        if not file_path.is_file():
            continue
        source = file_path.read_text(encoding="utf-8", errors="replace")
        has_protocol = _python_class_has_iteration_protocol(source, class_name)
        if has_protocol is None:
            continue
        found = True
        if has_protocol:
            return False
    return found


@dataclass(frozen=True)
class _PythonClassApi:
    found: bool
    methods: set[str]
    class_attrs: set[str]
    init_keywords: set[str]
    required_init_args: set[str]
    init_accepts_var_kwargs: bool


def _python_class_api(source: str, class_name: str) -> _PythonClassApi | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        methods = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        class_attrs: set[str] = set()
        for item in node.body:
            targets: list[ast.expr] = []
            if isinstance(item, ast.Assign):
                targets = list(item.targets)
            elif isinstance(item, ast.AnnAssign):
                targets = [item.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    class_attrs.add(target.id)
        init_keywords: set[str] = set()
        required_init_args: set[str] = set()
        init_accepts_var_kwargs = False
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                args = item.args
                positional = [arg.arg for arg in args.args[1:]]
                keyword_only = [arg.arg for arg in args.kwonlyargs]
                init_keywords.update(positional)
                init_keywords.update(keyword_only)
                positional_defaults = len(args.defaults)
                required_positional = positional[: max(0, len(positional) - positional_defaults)]
                required_init_args.update(required_positional)
                required_init_args.update(
                    arg.arg
                    for arg, default in zip(args.kwonlyargs, args.kw_defaults)
                    if default is None
                )
                init_accepts_var_kwargs = args.kwarg is not None
                break
        return _PythonClassApi(
            found=True,
            methods=methods,
            class_attrs=class_attrs,
            init_keywords=init_keywords,
            required_init_args=required_init_args,
            init_accepts_var_kwargs=init_accepts_var_kwargs,
        )
    return None


def _repo_class_apis(repo_dir: Path, class_name: str, *, timeout: int) -> list[_PythonClassApi]:
    rc, hits = _run_git(
        [
            "git", "grep", "-n", "--no-color",
            "-E", rf"^class[[:space:]]+{re.escape(class_name)}\b",
            "--", "*.py",
        ],
        repo_dir,
        timeout=timeout,
    )
    if rc != 0 or not hits.strip():
        return []
    apis: list[_PythonClassApi] = []
    for hit in hits.splitlines():
        path, _, _rest = hit.partition(":")
        if not path:
            continue
        file_path = repo_dir / path
        if not file_path.is_file():
            continue
        api = _python_class_api(
            file_path.read_text(encoding="utf-8", errors="replace"),
            class_name,
        )
        if api is not None:
            apis.append(api)
    return apis


def _untracked_python_files(repo_dir: Path, *, timeout: int = 60) -> list[str]:
    rc, out = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        repo_dir,
        timeout=timeout,
    )
    if rc != 0:
        return []
    return [
        line.strip().replace("\\", "/")
        for line in out.splitlines()
        if line.strip().endswith(".py")
    ]


def _extract_keyword_args(arg_text: str) -> set[str]:
    """Return only keyword arguments belonging to the outer call.

    ``arg_text`` comes from a deliberately lightweight call regex and can
    contain nested calls.  A flat ``name=`` search incorrectly attributes
    nested keywords (for example ``Dumper=`` in ``Error(yaml.dump(...))``) to
    the outer constructor.  Split at top-level commas while respecting Python
    delimiters and strings, then accept a keyword only at the start of a
    top-level argument.
    """
    arguments: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    triple = False
    escaped = False
    index = 0

    while index < len(arg_text):
        char = arg_text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif triple and arg_text.startswith(quote * 3, index):
                quote = None
                triple = False
                index += 2
            elif not triple and char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            triple = arg_text.startswith(char * 3, index)
            if triple:
                index += 2
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            arguments.append(arg_text[start:index])
            start = index + 1
        index += 1

    arguments.append(arg_text[start:])
    keywords: set[str] = set()
    for argument in arguments:
        match = re.match(
            r"\s*([A-Za-z_][_A-Za-z0-9]*)\s*=(?!=)",
            argument,
        )
        if match:
            keywords.add(match.group(1))
    return keywords


def _python_call_class_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name) and func.id[:1].isupper():
        return func.id
    if isinstance(func, ast.Attribute) and func.attr[:1].isupper():
        return func.attr
    return None


def _python_config_subscript_key(node: ast.Subscript) -> str | None:
    value = node.value
    is_config = isinstance(value, ast.Name) and value.id == "config"
    is_runtime_config = (
        isinstance(value, ast.Attribute)
        and value.attr == "runtime_config"
        and isinstance(value.value, ast.Name)
        and value.value.id == "config"
    )
    if not (is_config or is_runtime_config):
        return None
    slc = node.slice
    if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
        return slc.value
    return None


def check_python_noniterable_class_loop(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect newly added loops over repo-defined non-iterable class instances.

    This catches the common helper-API hallucination where a callable helper is
    used as ``for _ in Retry(...):`` even though the class exposes no iteration
    protocol. Compile/import cannot catch this unless the code path executes.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    errors: list[BuildError] = []
    seen: set[tuple[str, ...]] = set()
    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if not file_path.endswith(".py") or is_test_file(file_path):
            continue
        for hunk in hunks:
            for line in hunk:
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                text = line[1:].strip()
                match = _PY_CLASS_ITERATION_CALL_RE.search(text)
                if not match:
                    continue
                class_name = match.group(1)
                sig = (file_path, class_name)
                if sig in seen:
                    continue
                if not _repo_class_is_known_noniterable(
                    repo_dir, class_name, timeout=timeout
                ):
                    continue
                seen.add(sig)
                errors.append(BuildError(
                    file=file_path,
                    line=None,
                    message=(
                        f"python helper protocol misuse: new code iterates over "
                        f"`{class_name}(...)`, but the repo-defined class "
                        f"`{class_name}` has no __iter__/__next__/__getitem__ "
                        "method. Read the helper API and call it using an "
                        "existing supported pattern; do not invent iterable/"
                        "context-manager semantics for helper classes."
                    ),
                    raw=text,
                ))
    return errors


def render_python_noniterable_class_loop_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Python helper-protocol gate found iteration over non-iterable classes:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message} (line: {err.raw.strip()[:120]})")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


def check_python_helper_api_usage(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect invented constructor keywords or methods on repo-defined classes."""
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    errors: list[BuildError] = []
    seen: set[tuple[str, ...]] = set()

    def _check_constructor_kwargs(
        file_path: str,
        class_name: str,
        kwargs: set[str],
        raw: str,
        line: int | None = None,
        positional_count: int | None = None,
    ) -> None:
        if not kwargs and positional_count is None:
            return
        apis = _repo_class_apis(repo_dir, class_name, timeout=timeout)
        if not apis:
            return
        if kwargs:
            allowed = any(
                api.init_accepts_var_kwargs or kwargs.issubset(api.init_keywords)
                for api in apis
            )
            if not allowed:
                unknown = sorted(
                    kwargs - set().union(*(api.init_keywords for api in apis))
                )
                sig = (file_path, class_name, "unknown-keywords", ",".join(unknown))
                if sig not in seen:
                    seen.add(sig)
                    errors.append(BuildError(
                        file=file_path,
                        line=line,
                        message=(
                            f"python helper API misuse: new code calls "
                            f"`{class_name}(...)` with unsupported keyword(s) "
                            f"{unknown}. Read the repo-defined class constructor "
                            "and use only real parameters; do not invent helper "
                            "configuration keywords."
                        ),
                        raw=raw,
                    ))
        if positional_count is None:
            return
        missing_required_sets: list[set[str]] = []
        for api in apis:
            remaining_required = set(api.required_init_args)
            # Positional args satisfy required positional/keyword parameters in
            # declaration order; if parsing came from regex text we skip this
            # check because positional_count is unavailable.
            ordered_required = [
                name for name in api.init_keywords if name in remaining_required
            ]
            for name in ordered_required[:positional_count]:
                remaining_required.discard(name)
            remaining_required -= kwargs
            if remaining_required:
                missing_required_sets.append(remaining_required)
        if missing_required_sets and len(missing_required_sets) == len(apis):
            missing = sorted(set.intersection(*missing_required_sets))
            if not missing:
                missing = sorted(set.union(*missing_required_sets))
            sig = (file_path, class_name, "missing-required", ",".join(missing))
            if sig in seen:
                return
            seen.add(sig)
            errors.append(BuildError(
                file=file_path,
                line=line,
                message=(
                    f"python helper API misuse: new code calls "
                    f"`{class_name}(...)` without required constructor "
                    f"argument(s) {missing}. Read the repo-defined class "
                    "constructor and supply the real required arguments; do "
                    "not assume optional/default-only construction."
                ),
                raw=raw,
            ))

    def _check_inline_method(
        file_path: str,
        class_name: str,
        method: str,
        raw: str,
        line: int | None = None,
    ) -> None:
        apis = _repo_class_apis(repo_dir, class_name, timeout=timeout)
        if not apis:
            return
        if any(method in api.methods for api in apis):
            return
        sig = (file_path, class_name, method)
        if sig in seen:
            return
        seen.add(sig)
        errors.append(BuildError(
            file=file_path,
            line=line,
            message=(
                f"python helper API misuse: new code calls "
                f"`{class_name}(...).{method}(...)`, but the "
                f"repo-defined class `{class_name}` has no method "
                f"`{method}`. Read existing call sites and use the "
                "actual helper invocation pattern."
            ),
            raw=raw,
        ))

    def _check_class_attribute(
        file_path: str,
        class_name: str,
        attr: str,
        raw: str,
        line: int | None = None,
    ) -> None:
        apis = _repo_class_apis(repo_dir, class_name, timeout=timeout)
        if not apis:
            return
        if any(attr in api.class_attrs or attr in api.methods for api in apis):
            return
        sig = (file_path, class_name, "class-attr", attr)
        if sig in seen:
            return
        seen.add(sig)
        errors.append(BuildError(
            file=file_path,
            line=line,
            message=(
                f"python helper API misuse: new code references "
                f"`{class_name}.{attr}`, but the repo-defined class "
                f"`{class_name}` does not expose that class attribute or "
                "nested type. Import/use the actual symbol where it is "
                "defined; do not assume helper exceptions or constants are "
                "nested under the helper class."
            ),
            raw=raw,
        ))

    def _scan_text(file_path: str, text: str, raw: str) -> None:
        for match in _PY_CLASS_CALL_RE.finditer(text):
            class_name, args_text = match.group(1), match.group(2)
            _check_constructor_kwargs(
                file_path,
                class_name,
                _extract_keyword_args(args_text),
                raw,
            )
        for match in _PY_CLASS_INLINE_METHOD_RE.finditer(text):
            class_name, method = match.group(1), match.group(2)
            _check_inline_method(file_path, class_name, method, raw)

    def _scan_ast(file_path: str, source: str) -> None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id[:1].isupper()
            ):
                _check_class_attribute(
                    file_path,
                    node.value.id,
                    node.attr,
                    ast.get_source_segment(source, node) or (
                        f"{node.value.id}.{node.attr}"
                    ),
                    getattr(node, "lineno", None),
                )
            if not isinstance(node, ast.Call):
                continue
            class_name = _python_call_class_name(node.func)
            if class_name:
                kwargs = {
                    kw.arg
                    for kw in node.keywords
                    if kw.arg is not None
                }
                raw = ast.get_source_segment(source, node) or f"{class_name}(...)"
                _check_constructor_kwargs(
                    file_path,
                    class_name,
                    kwargs,
                    raw,
                    getattr(node, "lineno", None),
                    positional_count=sum(
                        1
                        for arg in node.args
                        if not (
                            isinstance(arg, ast.Starred)
                        )
                    ),
                )
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
            ):
                inner_class_name = _python_call_class_name(node.func.value.func)
                if inner_class_name:
                    raw = ast.get_source_segment(source, node) or (
                        f"{inner_class_name}(...).{node.func.attr}(...)"
                    )
                    _check_inline_method(
                        file_path,
                        inner_class_name,
                        node.func.attr,
                        raw,
                        getattr(node, "lineno", None),
                    )

    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if not file_path.endswith(".py") or is_test_file(file_path):
            continue
        for hunk in hunks:
            added_lines: list[str] = []
            for line in hunk:
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                text = line[1:].strip()
                added_lines.append(text)
                _scan_text(file_path, text, text)
            if added_lines:
                blob = " ".join(added_lines)
                _scan_text(file_path, blob, blob[:500])
    for file_path in _untracked_python_files(repo_dir, timeout=timeout):
        if is_test_file(file_path):
            continue
        source_path = repo_dir / file_path
        if not source_path.is_file():
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        _scan_ast(file_path, source)
        _scan_text(file_path, source, source[:500])
    return errors


def render_python_helper_api_usage_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Python helper-API gate found invented class keywords/methods:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message} (line: {err.raw.strip()[:120]})")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


def check_python_config_subscript_fallback(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect newly added direct config section indexing without fallback.

    A moved/shared helper often runs in more contexts than its original caller.
    Direct ``config["section"]`` or ``config.runtime_config["section"]`` access
    turns an absent optional section into a runtime KeyError that compile/import
    cannot catch. New code should use ``.get(...)`` with an explicit default or
    otherwise handle absence near the lookup.
    """
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    errors: list[BuildError] = []
    seen: set[tuple[str, str]] = set()

    def _add(file_path: str, key: str, raw: str, line: int | None = None) -> None:
        sig = (file_path, key)
        if sig in seen:
            return
        seen.add(sig)
        errors.append(BuildError(
            file=file_path,
            line=line,
            message=(
                "python config fallback gate: new code directly indexes "
                f"a configuration section `{key}` via `config[...]` or "
                "`config.runtime_config[...]`. Use `.get(..., {})`, an "
                "explicit default, or a local fallback path unless the "
                "requirement explicitly says a missing optional section must "
                "raise KeyError."
            ),
            raw=raw,
        ))

    def _add_keyerror(file_path: str, raw: str, line: int | None = None) -> None:
        sig = (file_path, "explicit-keyerror")
        if sig in seen:
            return
        seen.add(sig)
        errors.append(BuildError(
            file=file_path,
            line=line,
            message=(
                "python config fallback gate: new code explicitly raises "
                "KeyError for a missing configuration value. Use a repo-"
                "consistent safe default, optional return, or local fallback "
                "unless the requirement explicitly says missing optional "
                "configuration must raise KeyError."
            ),
            raw=raw,
        ))

    def _scan_text(file_path: str, text: str) -> None:
        for match in _PY_CONFIG_SUBSCRIPT_RE.finditer(text):
            _add(file_path, match.group(2), text)
        if "raise KeyError" in text and _PY_CONFIG_KEYERROR_HINT_RE.search(text):
            _add_keyerror(file_path, text)

    def _scan_ast(file_path: str, source: str) -> None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            key = _python_config_subscript_key(node)
            if key is None:
                continue
            raw = ast.get_source_segment(source, node) or f"config[{key!r}]"
            _add(file_path, key, raw, getattr(node, "lineno", None))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc
            is_keyerror = (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "KeyError"
            ) or (
                isinstance(exc, ast.Name)
                and exc.id == "KeyError"
            )
            if not is_keyerror:
                continue
            raw = ast.get_source_segment(source, node) or "raise KeyError(...)"
            if _PY_CONFIG_KEYERROR_HINT_RE.search(raw):
                _add_keyerror(file_path, raw, getattr(node, "lineno", None))

    for file_path, hunks in _iter_diff_file_hunks(diff_text):
        if not file_path.endswith(".py") or is_test_file(file_path):
            continue
        for hunk in hunks:
            added_lines: list[str] = []
            for line in hunk:
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                text = line[1:].strip()
                added_lines.append(text)
                _scan_text(file_path, text)
            if added_lines:
                _scan_text(file_path, " ".join(added_lines))
    for file_path in _untracked_python_files(repo_dir, timeout=timeout):
        if is_test_file(file_path):
            continue
        source_path = repo_dir / file_path
        if not source_path.is_file():
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        _scan_ast(file_path, source)
        _scan_text(file_path, source)
    return errors


def render_python_config_subscript_fallback_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Python config-fallback gate found direct config section indexing:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message} (line: {err.raw.strip()[:120]})")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)


def _base_python_class_apis(
    repo_dir: Path,
    class_name: str,
    *,
    ref: str,
    timeout: int,
) -> list[_PythonClassApi]:
    rc, files = _run_git(
        ["git", "ls-files", "--", "*.py"],
        repo_dir,
        timeout=timeout,
    )
    if rc != 0:
        return []
    apis: list[_PythonClassApi] = []
    for rel in files.splitlines():
        rel = rel.strip().replace("\\", "/")
        if not rel or is_test_file(rel):
            continue
        rc_show, source = _run_git(
            ["git", "show", f"{ref}:{rel}"],
            repo_dir,
            timeout=timeout,
        )
        if rc_show != 0 or not source:
            continue
        api = _python_class_api(source, class_name)
        if api:
            apis.append(api)
    return apis


def _current_python_classes(source: str) -> list[tuple[str, int, set[str]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    classes: list[tuple[str, int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        classes.append((node.name, getattr(node, "lineno", 1), methods))
    return classes


def check_python_moved_class_dunder_methods(
    repo_dir: Path,
    base_commit: str | None = None,
    timeout: int = 90,
) -> list[BuildError]:
    """Detect moved/recreated Python classes that drop public dunder methods."""
    repo_dir = Path(repo_dir)
    ref = base_commit or "HEAD"
    rc, diff_text = _run_git(["git", "diff", ref, "--", "."], repo_dir, timeout=timeout)
    if rc != 0 and not diff_text.strip():
        return []

    changed_files = {
        file_path
        for file_path, _hunks in _iter_diff_file_hunks(diff_text)
        if file_path.endswith(".py") and not is_test_file(file_path)
    }
    changed_files.update(
        path
        for path in _untracked_python_files(repo_dir, timeout=timeout)
        if not is_test_file(path)
    )

    errors: list[BuildError] = []
    seen: set[tuple[str, str, str]] = set()
    for file_path in sorted(changed_files):
        source_path = repo_dir / file_path
        if not source_path.is_file():
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        for class_name, line, methods in _current_python_classes(source):
            base_apis = _base_python_class_apis(
                repo_dir, class_name, ref=ref, timeout=timeout
            )
            if not base_apis:
                continue
            base_dunders = set().union(
                *(api.methods & _PUBLIC_DUNDER_METHODS for api in base_apis)
            )
            if not base_dunders:
                continue
            missing = sorted(base_dunders - methods)
            if not missing:
                continue
            sig = (file_path, class_name, ",".join(missing))
            if sig in seen:
                continue
            seen.add(sig)
            errors.append(BuildError(
                file=file_path,
                line=line,
                message=(
                    f"python moved-class API gate: `{class_name}` is recreated "
                    "or moved but drops public dunder/operator method(s) "
                    f"{missing} that existed on the base class. Preserve "
                    "operator/container/context-manager behavior unless the "
                    "requirement explicitly removes it."
                ),
                raw=f"class {class_name}",
            ))
    return errors


def render_python_moved_class_dunder_methods_for_feedback(
    errors: list[BuildError], limit: int = 20
) -> str:
    if not errors:
        return ""
    lines = ["Python moved-class API gate found dropped public dunder methods:"]
    for err in errors[:limit]:
        lines.append(f"- {err.file}: {err.message} (line: {err.raw.strip()[:120]})")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)
