"""Patch artifact verification.

This gate checks whether the concrete patch artifact still matches the plan.
It deliberately runs before language build/test verification: missing files,
empty diffs, unresolved relative imports, and absent planned symbols are
structural defects that should drive a repatch even on repositories without a
compile step.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.models.patch import PatchPlan
from src.orchestrator.consistency_checks import is_test_file


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_JS_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+\s+from\s+)?|export\s+[^'\"]+\s+from\s+|"
    r"require\s*\()\s*['\"]([^'\"]+)['\"]"
)
_JS_NAMED_IMPORT_RE = re.compile(
    r"(?:import|export)\s*\{(?P<names>[^}]+)\}\s*from\s*['\"](?P<spec>[^'\"]+)['\"]"
)
_PY_FROM_RE = re.compile(
    r"^\s*from\s+(?P<module>[A-Za-z_][\w.]*|\.+[A-Za-z_][\w.]*)\s+import\s+"
    r"(?P<names>[^#\n]+)",
    re.MULTILINE,
)
_PY_IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<modules>[A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)",
    re.MULTILINE,
)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_SYMBOL_SKIP = {
    "class",
    "const",
    "def",
    "func",
    "function",
    "interface",
    "let",
    "struct",
    "type",
    "var",
}


@dataclass(frozen=True)
class ArtifactFinding:
    """One artifact-level mismatch between patch plan and concrete diff/tree."""

    code: str
    file: str
    message: str
    symbol: str = ""
    target: str = ""
    raw: str = ""

    def signature(self) -> str:
        return "::".join(
            [
                self.code,
                _norm_path(self.file),
                self.symbol.strip(),
                self.target.strip(),
            ]
        ).lower()


@dataclass
class ArtifactVerificationResult:
    """Result of verifying patch artifacts against the plan and working tree."""

    ok: bool
    findings: list[ArtifactFinding] = field(default_factory=list)
    diff_paths: list[str] = field(default_factory=list)
    planned_required_files: list[str] = field(default_factory=list)
    empty_patch: bool = False

    def to_log(self) -> dict:
        return {
            "ok": self.ok,
            "empty_patch": self.empty_patch,
            "diff_paths": self.diff_paths,
            "planned_required_files": self.planned_required_files,
            "findings": [
                {
                    "code": f.code,
                    "file": f.file,
                    "symbol": f.symbol,
                    "target": f.target,
                    "message": f.message,
                    "raw": f.raw,
                }
                for f in self.findings
            ],
        }


def parse_diff_paths(diff_text: str) -> list[str]:
    """Return b-side paths from a unified git diff."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in (diff_text or "").splitlines():
        match = _DIFF_GIT_RE.match(line)
        if not match:
            continue
        path = _norm_path(match.group(2).strip().strip('"'))
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def verify_patch_artifacts(
    repo_dir: Path,
    patch_plan: PatchPlan | None,
    diff_text: str,
) -> ArtifactVerificationResult:
    """Verify patch-plan coverage, required symbols, and import targets."""
    repo_dir = Path(repo_dir)
    diff_paths = parse_diff_paths(diff_text)
    added_import_lines = _added_import_lines_by_file(diff_text)
    findings: list[ArtifactFinding] = []
    if not diff_paths:
        findings.append(
            ArtifactFinding(
                code="NO_EFFECT_PATCH",
                file="(patch.diff)",
                message="patch.diff contains no changed files",
            )
        )

    planned_required_files: list[str] = []
    if patch_plan is not None:
        for edit in patch_plan.edits:
            path = _norm_path(edit.filepath)
            if is_test_file(path):
                continue
            if edit.reference_only and not getattr(edit, "creates_new_file", False):
                expected_diff = False
            else:
                expected_diff = bool(
                    getattr(edit, "expected_diff_required", True)
                    or getattr(edit, "creates_new_file", False)
                )
            if edit.reference_only and not expected_diff:
                continue
            if expected_diff:
                planned_required_files.append(path)
                if path not in diff_paths:
                    findings.append(
                        ArtifactFinding(
                            code="PLAN_DIFF_MISMATCH",
                            file=path,
                            message=(
                                "patch_plan requires this file to change, "
                                "but it is absent from patch.diff"
                            ),
                        )
                    )
            if getattr(edit, "creates_new_file", False) and not (repo_dir / path).is_file():
                findings.append(
                    ArtifactFinding(
                        code="REQUIRED_FILE_MISSING",
                        file=path,
                        message="patch_plan marks this as a new file, but it does not exist",
                    )
                )
            for symbol in getattr(edit, "expected_symbols", []) or []:
                if not _symbol_exists(repo_dir / path, symbol):
                    findings.append(
                        ArtifactFinding(
                            code="SYMBOL_TARGET_MISSING",
                            file=path,
                            symbol=symbol,
                            message=f"expected symbol is not defined in {path}: {symbol}",
                        )
                    )

    for path in diff_paths:
        findings.extend(_check_import_targets(repo_dir, path, added_import_lines.get(path)))

    deduped: list[ArtifactFinding] = []
    seen: set[str] = set()
    for finding in findings:
        sig = finding.signature()
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(finding)

    return ArtifactVerificationResult(
        ok=not deduped,
        findings=deduped,
        diff_paths=diff_paths,
        planned_required_files=planned_required_files,
        empty_patch=not diff_paths,
    )


def render_artifact_feedback(
    findings: list[ArtifactFinding],
    repo_dir: Path | None = None,
) -> str:
    """Render artifact findings as planner feedback for a repatch round."""
    if not findings:
        return ""
    repo_dir = Path(repo_dir) if repo_dir is not None else None
    lines = [
        "Patch artifact verification failed before build/eval.",
        "Fix these concrete plan/diff mismatches without dropping the intended requirements:",
    ]
    for finding in findings:
        extra = ""
        if finding.symbol:
            extra += f" symbol={finding.symbol}"
        if finding.target:
            extra += f" target={finding.target}"
        lines.append(
            f"- {finding.code} file={finding.file}{extra}: {finding.message}"
        )
        if repo_dir is not None:
            lines.extend(f"  guidance: {line}" for line in _artifact_guidance(repo_dir, finding))
    return "\n".join(lines)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _added_import_lines_by_file(diff_text: str) -> dict[str, list[str]]:
    """Return added import-like lines per b-side file from a unified diff.

    Import verification should block newly introduced unresolved imports, not
    every unresolved import that happened to exist in a file the patch touched.
    Some SWE-bench repos contain dynamic or optional imports that do not
    resolve by static path alone; if the patch did not add that line, artifact
    verification should leave it to build/eval instead of creating a false
    positive.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in (diff_text or "").splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match:
            current = _norm_path(match.group(2).strip().strip('"'))
            out.setdefault(current, [])
            continue
        if current is None or not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        stripped = added.strip()
        if (
            stripped.startswith(("import ", "from "))
            or " from " in stripped
            or stripped.startswith("export ")
        ):
            out.setdefault(current, []).append(added)
    return out


def _symbol_exists(path: Path, symbol: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if symbol and symbol in text:
        return True
    identifiers = [tok for tok in _IDENT_RE.findall(symbol or "") if tok not in _SYMBOL_SKIP]
    if not identifiers:
        return True
    name = identifiers[-1]
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def _check_import_targets(
    repo_dir: Path,
    rel_path: str,
    added_import_lines: list[str] | None = None,
) -> list[ArtifactFinding]:
    path = repo_dir / rel_path
    if not path.is_file():
        return []
    suffix = path.suffix.lower()
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return _check_js_imports(repo_dir, rel_path, path, added_import_lines)
    if suffix == ".py":
        return _check_python_imports(repo_dir, rel_path, path, added_import_lines)
    return []


def _check_js_imports(
    repo_dir: Path,
    rel_path: str,
    path: Path,
    added_import_lines: list[str] | None = None,
) -> list[ArtifactFinding]:
    text = "\n".join(added_import_lines) if added_import_lines is not None else path.read_text(encoding="utf-8", errors="replace")
    findings: list[ArtifactFinding] = []
    for match in _JS_IMPORT_RE.finditer(text):
        spec = match.group(1).strip()
        if not spec.startswith("."):
            continue
        target_path = _resolve_js_module_path(path.parent, spec)
        if target_path is None:
            findings.append(
                ArtifactFinding(
                    code="IMPORT_TARGET_MISSING",
                    file=rel_path,
                    target=spec,
                    message=f"relative JS/TS import target does not resolve: {spec}",
                    raw=match.group(0),
                )
            )
    for match in _JS_NAMED_IMPORT_RE.finditer(text):
        spec = match.group("spec").strip()
        if not spec.startswith("."):
            continue
        target_path = _resolve_js_module_path(path.parent, spec)
        if target_path is None:
            continue
        for symbol in _parse_js_named_imports(match.group("names")):
            if not _js_module_exports_symbol(target_path, symbol, seen=set()):
                findings.append(
                    ArtifactFinding(
                        code="IMPORT_SYMBOL_MISSING",
                        file=rel_path,
                        symbol=symbol,
                        target=spec,
                        message=(
                            "JS/TS named import does not resolve from module "
                            f"{spec}: {symbol}"
                        ),
                        raw=match.group(0),
                    )
                )
    return findings


def _resolve_js_module(base_dir: Path, spec: str) -> bool:
    return _resolve_js_module_path(base_dir, spec) is not None


def _resolve_js_module_path(base_dir: Path, spec: str) -> Path | None:
    candidate = (base_dir / spec).resolve()
    suffixes = (
        "",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mts",
        ".cts",
        ".mjs",
        ".cjs",
        ".json",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    )
    for suffix in suffixes:
        fp = Path(str(candidate) + suffix)
        if fp.is_file():
            return fp
    if candidate.is_dir():
        for name in (
            "index.ts",
            "index.tsx",
            "index.js",
            "index.jsx",
            "index.mts",
            "index.cts",
            "index.mjs",
            "index.cjs",
            "index.json",
        ):
            fp = candidate / name
            if fp.is_file():
                return fp
    return None


def _parse_js_named_imports(names: str) -> list[str]:
    out: list[str] = []
    for chunk in names.split(","):
        part = chunk.strip()
        if not part:
            continue
        if " as " in part:
            part = part.split(" as ", 1)[0].strip()
        if part and part not in {"default", "*"}:
            out.append(part)
    return out


def _js_module_exports_symbol(path: Path, symbol: str, seen: set[Path]) -> bool:
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    direct_patterns = (
        rf"(?m)^\s*export\s+(?:const|let|var|function|class)\s+{re.escape(symbol)}\b",
        rf"(?m)^\s*export\s*\{{[^}}]*\b{re.escape(symbol)}\b[^}}]*\}}(?!\s*from)",
        rf"(?m)^\s*export\s+default\s+{re.escape(symbol)}\b",
    )
    if any(re.search(pattern, text) for pattern in direct_patterns):
        return True

    for match in re.finditer(
        r"(?m)^\s*export\s*\{(?P<names>[^}]+)\}\s*from\s*['\"](?P<spec>[^'\"]+)['\"]",
        text,
    ):
        spec = match.group("spec").strip()
        if not spec.startswith("."):
            continue
        exported_names = _parse_js_named_imports(match.group("names"))
        if symbol not in exported_names:
            continue
        target = _resolve_js_module_path(path.parent, spec)
        if target is not None and _js_module_exports_symbol(target, symbol, seen):
            return True

    for match in re.finditer(
        r"(?m)^\s*export\s+\*\s+from\s+['\"](?P<spec>[^'\"]+)['\"]",
        text,
    ):
        spec = match.group("spec").strip()
        if not spec.startswith("."):
            continue
        target = _resolve_js_module_path(path.parent, spec)
        if target is not None and _js_module_exports_symbol(target, symbol, seen):
            return True
    return False


def _check_python_imports(
    repo_dir: Path,
    rel_path: str,
    path: Path,
    added_import_lines: list[str] | None = None,
) -> list[ArtifactFinding]:
    text = "\n".join(added_import_lines) if added_import_lines is not None else path.read_text(encoding="utf-8", errors="replace")
    findings: list[ArtifactFinding] = []
    for match in _PY_IMPORT_RE.finditer(text):
        for module in match.group("modules").split(","):
            mod = module.strip().split(" as ", 1)[0].strip()
            if _should_check_python_module(repo_dir, mod) and not _python_module_exists(repo_dir, mod):
                findings.append(
                    ArtifactFinding(
                        code="IMPORT_TARGET_MISSING",
                        file=rel_path,
                        target=mod,
                        message=f"Python import target does not resolve in repo: {mod}",
                        raw=match.group(0),
                    )
                )
    for match in _PY_FROM_RE.finditer(text):
        module = match.group("module").strip()
        if module.startswith("."):
            # Relative Python imports need package context; leave them to
            # Python's import smoke/build checks to avoid false positives.
            continue
        if _should_check_python_module(repo_dir, module):
            missing = _missing_python_from_import_names(
                repo_dir, module, match.group("names")
            )
            if missing is None:
                imported_names = _parse_python_imported_names(match.group("names"))
                findings.append(
                    ArtifactFinding(
                        code="IMPORT_TARGET_MISSING",
                        file=rel_path,
                        symbol=", ".join(imported_names),
                        target=module,
                        message=(
                            "Python from-import module does not resolve in repo: "
                            f"{module}"
                            + (
                                f" while importing {', '.join(imported_names)}"
                                if imported_names else ""
                            )
                        ),
                        raw=match.group(0),
                    )
                )
            elif missing:
                findings.append(
                    ArtifactFinding(
                        code="IMPORT_SYMBOL_MISSING",
                        file=rel_path,
                        symbol=", ".join(missing),
                        target=module,
                        message=(
                            "Python from-import name(s) do not resolve in repo "
                            f"module {module}: {', '.join(missing)}"
                        ),
                        raw=match.group(0),
                    )
                )
    return findings


def _should_check_python_module(repo_dir: Path, module: str) -> bool:
    if not module or module.startswith("."):
        return False
    top = module.split(".", 1)[0]
    # Only verify imports that look like in-repository modules. Third-party
    # packages are resolved by dependency installation/build checks.
    return (repo_dir / f"{top}.py").is_file() or (repo_dir / top).is_dir()


def _python_module_exists(repo_dir: Path, module: str) -> bool:
    rel = Path(*module.split("."))
    return (
        (repo_dir / f"{rel.as_posix()}.py").is_file()
        or (repo_dir / rel / "__init__.py").is_file()
    )


def _python_module_source_path(repo_dir: Path, module: str) -> Path | None:
    rel = Path(*module.split("."))
    py = repo_dir / f"{rel.as_posix()}.py"
    if py.is_file():
        return py
    init = repo_dir / rel / "__init__.py"
    if init.is_file():
        return init
    return None


def _python_source_defines_name(path: Path, name: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if re.search(
        rf"(?m)^\s*(?:class|def|async\s+def)\s+{re.escape(name)}\b|"
        rf"^\s*{re.escape(name)}\s*(?::[^=]+)?=",
        text,
    ) is not None:
        return True

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                exported = alias.asname or alias.name.split(".", 1)[0]
                if exported == name:
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exported = alias.asname or alias.name.split(".", 1)[0]
                if exported == name:
                    return True
    return False


def _parse_python_imported_names(names: str) -> list[str]:
    return [
        raw.strip().split(" as ", 1)[0].strip()
        for raw in names.replace("(", "").replace(")", "").split(",")
        if raw.strip()
    ]


def _python_from_import_resolves(repo_dir: Path, module: str, names: str) -> bool:
    missing = _missing_python_from_import_names(repo_dir, module, names)
    return missing == []


def _missing_python_from_import_names(
    repo_dir: Path, module: str, names: str
) -> list[str] | None:
    """Resolve `from module import names` for modules and namespace packages.

    A package directory without `__init__.py` can still be a Python 3 namespace
    package.  For `from scripts.monitoring import haproxy_monitor`, accepting
    `scripts/monitoring/haproxy_monitor.py` is therefore correct even when
    `scripts/monitoring/__init__.py` is absent.

    Returns:
    - None when the source module/package itself does not exist.
    - [] when all requested names resolve.
    - a list of missing imported names when the module exists but lacks them.
    """
    base = repo_dir / Path(*module.split("."))
    imported_names = _parse_python_imported_names(names)
    source_path = _python_module_source_path(repo_dir, module)
    if "*" in imported_names:
        return [] if source_path is not None or base.is_dir() else None
    if not imported_names:
        return [] if source_path is not None or base.is_dir() else None

    if source_path is not None:
        missing: list[str] = []
        for name in imported_names:
            if not _IDENT_RE.fullmatch(name):
                continue
            if _python_source_defines_name(source_path, name):
                continue
            if (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file():
                continue
            missing.append(name)
        return missing

    if not base.is_dir():
        return None
    missing = []
    for name in imported_names:
        if not _IDENT_RE.fullmatch(name):
            continue
        if (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file():
            continue
        missing.append(name)
    return missing


def _artifact_guidance(repo_dir: Path, finding: ArtifactFinding) -> list[str]:
    """Return deterministic repair guidance for import-related findings."""
    if finding.code not in {"IMPORT_TARGET_MISSING", "IMPORT_SYMBOL_MISSING"}:
        return []

    lines: list[str] = []
    if finding.code == "IMPORT_TARGET_MISSING":
        lines.append(
            "The import target module does not exist in this repo; do not keep "
            "or invent that import path unless the patch also creates the "
            "corresponding module file."
        )
        if finding.target.startswith(".") and finding.file.endswith(
            (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
        ):
            candidates = _candidate_js_relative_targets(repo_dir, finding.file, finding.target)
            if candidates:
                lines.append(
                    "Closest existing sibling module targets from this directory: "
                    + ", ".join(candidates)
                )

    symbols = _finding_import_symbols(finding)
    if not symbols:
        return lines

    candidates = _candidate_python_symbol_locations(
        repo_dir,
        symbols,
        exclude_path=finding.file,
    )
    if candidates:
        lines.append(
            "Existing in-repo definitions for requested imported name(s): "
            + "; ".join(candidates)
        )
    else:
        lines.append(
            "No existing in-repo definition was found for requested imported "
            "name(s); the patch must define/export them in a real module or "
            "remove the invalid import."
        )
    return lines


def _finding_import_symbols(finding: ArtifactFinding) -> list[str]:
    if finding.symbol:
        return [
            item.strip()
            for item in finding.symbol.split(",")
            if item.strip() and _IDENT_RE.fullmatch(item.strip())
        ]
    if finding.raw:
        match = _PY_FROM_RE.search(finding.raw)
        if match:
            return [
                item
                for item in _parse_python_imported_names(match.group("names"))
                if _IDENT_RE.fullmatch(item)
            ]
    return []


def _candidate_js_relative_targets(
    repo_dir: Path,
    rel_path: str,
    spec: str,
    *,
    limit: int = 5,
) -> list[str]:
    path = repo_dir / rel_path
    base_dir = path.parent
    if not base_dir.is_dir():
        return []

    requested = spec.strip().replace("\\", "/")
    requested_name = Path(requested).name.lower()
    js_suffixes = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for child in base_dir.iterdir():
        candidate: str | None = None
        if child.is_file() and child.suffix.lower() in js_suffixes:
            candidate = f"./{child.stem}"
        elif child.is_dir():
            for index_name in (
                "index.js", "index.jsx", "index.ts", "index.tsx",
                "index.mjs", "index.cjs", "index.mts", "index.cts",
            ):
                if (child / index_name).is_file():
                    candidate = f"./{child.name}"
                    break
        if not candidate or candidate == requested or candidate in seen:
            continue
        seen.add(candidate)
        ratio = difflib.SequenceMatcher(
            None, requested_name, Path(candidate).name.lower()
        ).ratio()
        if ratio < 0.35:
            continue
        scored.append((ratio, candidate))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _, candidate in scored[:limit]]


def _candidate_python_symbol_locations(
    repo_dir: Path,
    symbols: list[str],
    *,
    exclude_path: str = "",
    limit: int = 8,
) -> list[str]:
    if not symbols:
        return []
    repo_dir = Path(repo_dir)
    exclude = _norm_path(exclude_path)
    out: list[str] = []
    seen: set[str] = set()
    skip_parts = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__"}
    for path in repo_dir.rglob("*.py"):
        try:
            rel = _norm_path(path.relative_to(repo_dir).as_posix())
        except ValueError:
            continue
        if rel == exclude or any(part in skip_parts for part in path.relative_to(repo_dir).parts):
            continue
        definitions = _python_symbol_definition_lines(path, symbols)
        if not definitions:
            continue
        module = _python_module_name_for_path(repo_dir, path)
        for symbol in symbols:
            line_no = definitions.get(symbol)
            if line_no is None:
                continue
            text = f"{symbol} -> {rel}:{line_no} (module {module})"
            if text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                return out
    return out


def _python_symbol_definition_lines(path: Path, symbols: list[str]) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    wanted = set(symbols)
    out: dict[str, int] = {}
    pattern = re.compile(
        r"^\s*(?:class|def|async\s+def)\s+(?P<name>[A-Za-z_]\w*)\b|"
        r"^\s*(?P<assign>[A-Za-z_]\w*)\s*(?::[^=]+)?=",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        name = match.group("name") or match.group("assign")
        if name not in wanted or name in out:
            continue
        # The files scanned here are small enough that a direct count is
        # clearer than threading binary-search helpers into this gate.
        line_no = text.count("\n", 0, match.start()) + 1
        out[name] = line_no
        if len(out) == len(wanted):
            break
    return out


def _python_module_name_for_path(repo_dir: Path, path: Path) -> str:
    rel = path.relative_to(repo_dir)
    if rel.name == "__init__.py":
        rel = rel.parent
    else:
        rel = rel.with_suffix("")
    return ".".join(rel.parts)
