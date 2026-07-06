"""Patch artifact verification.

This gate checks whether the concrete patch artifact still matches the plan.
It deliberately runs before language build/test verification: missing files,
empty diffs, unresolved relative imports, and absent planned symbols are
structural defects that should drive a repatch even on repositories without a
compile step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.models.patch import PatchPlan


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_JS_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+\s+from\s+)?|export\s+[^'\"]+\s+from\s+|"
    r"require\s*\()\s*['\"]([^'\"]+)['\"]"
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
        findings.extend(_check_import_targets(repo_dir, path))

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


def render_artifact_feedback(findings: list[ArtifactFinding]) -> str:
    """Render artifact findings as planner feedback for a repatch round."""
    if not findings:
        return ""
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
    return "\n".join(lines)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


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


def _check_import_targets(repo_dir: Path, rel_path: str) -> list[ArtifactFinding]:
    path = repo_dir / rel_path
    if not path.is_file():
        return []
    suffix = path.suffix.lower()
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return _check_js_imports(repo_dir, rel_path, path)
    if suffix == ".py":
        return _check_python_imports(repo_dir, rel_path, path)
    return []


def _check_js_imports(repo_dir: Path, rel_path: str, path: Path) -> list[ArtifactFinding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[ArtifactFinding] = []
    for match in _JS_IMPORT_RE.finditer(text):
        spec = match.group(1).strip()
        if not spec.startswith("."):
            continue
        if not _resolve_js_module(path.parent, spec):
            findings.append(
                ArtifactFinding(
                    code="IMPORT_TARGET_MISSING",
                    file=rel_path,
                    target=spec,
                    message=f"relative JS/TS import target does not resolve: {spec}",
                    raw=match.group(0),
                )
            )
    return findings


def _resolve_js_module(base_dir: Path, spec: str) -> bool:
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
            return True
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
            if (candidate / name).is_file():
                return True
    return False


def _check_python_imports(repo_dir: Path, rel_path: str, path: Path) -> list[ArtifactFinding]:
    text = path.read_text(encoding="utf-8", errors="replace")
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
        if _should_check_python_module(repo_dir, module) and not _python_module_exists(repo_dir, module):
            findings.append(
                ArtifactFinding(
                    code="IMPORT_TARGET_MISSING",
                    file=rel_path,
                    target=module,
                    message=f"Python from-import module does not resolve in repo: {module}",
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
