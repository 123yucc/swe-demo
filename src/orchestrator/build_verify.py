"""
Post-patch build verification (code-driven, no LLM).

After the patch-generator applies SEARCH/REPLACE edits, the working tree may
*apply cleanly* yet still be broken: a renamed Go struct field left stale in a
sibling file, a config schema that references a type that was never defined,
an unexported method that base-commit tests still call.  Nothing in the patch
pipeline caught this 鈥?the generator only verifies that the edited file shows
up in ``git diff``.

This module is the deterministic backstop.  It compiles / collects the patched
tree in ``repo_dir`` (which is ``/app`` inside the SWE-bench docker image, so
the toolchain and dependencies are present) and reports compile/collection
errors as structured ``BuildError`` records.

IMPORTANT: methodological boundary: this NEVER runs ``before_repo_set_cmd``,
NEVER pulls in hidden gold test files, and does not inspect repository tests
for Python projects. Python verification is limited to changed production
modules: syntax compilation plus import smoke checks with the repository's
source roots on ``PYTHONPATH``.

Language coverage:
  * go      鈫?``go build`` on changed packages + ``go test -c`` on the same
              packages (compile evaluator-owned package tests without running
              them)
  * python  -> compile/import changed production modules only
  * node    鈫?skipped (plain JS has no compile step; observed failures are all
              Go/Python 鈥?do not block, just log)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.orchestrator.repo_executor import (
    docker_executor_enabled,
    executor_workdir,
    run_repo_command,
)

BuildSystem = Literal["go", "python", "node", "java", "unknown"]

# Return code from ``_run`` when the toolchain executable itself was not found
# (FileNotFoundError / OSError on subprocess spawn). Distinct from a non-zero
# rc produced by a toolchain that *did* run and rejected the code.
_RC_TOOLCHAIN_MISSING = 127


# 鈹€鈹€ Detection 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def detect_build_system(repo_dir: Path) -> BuildSystem:
    """Classify the repo's build system by marker files.

    Precedence: go.mod > python (pyproject/setup.py/setup.cfg) > java
    (pom.xml/build.gradle) > package.json.  Go takes precedence because a Go
    repo's static compile is the highest-value check; Python next; Java's
    marker is checked before ``package.json`` because a JVM repo with a
    ``package.json`` for front-end assets is still a JVM repo. ``package.json``
    only when no compiled-toolchain marker is present.

    NOTE 鈥?java is recognised here only so the phase-26 dynamic-grounding gate
    can dispatch to the JVM reproduction adapter. The post-patch build gate
    (``run_build_check``) deliberately does NOT compile java yet (it returns
    ``skipped`` like node); that is a separate decision.
    """
    if (repo_dir / "go.mod").is_file():
        return "go"
    for marker in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (repo_dir / marker).is_file():
            return "python"
    for marker in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if (repo_dir / marker).is_file():
            return "java"
    if (repo_dir / "package.json").is_file():
        return "node"
    return "unknown"


# 鈹€鈹€ Structured errors 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@dataclass(frozen=True)
class BuildError:
    """One compile / collection error extracted from build output."""

    file: str
    line: int | None
    message: str
    raw: str

    def signature(self) -> str:
        """Normalized identity used for baseline diffing.

        Excludes the line number (a pre-existing error may shift lines after a
        patch) and keeps the file plus the leading, most-distinctive portion of
        the message.  Two errors with the same file + same message head are
        considered the same defect.
        """
        norm_file = self.file.replace("\\", "/").strip()
        head = self.message.strip()
        # Drop trailing positional noise ("in struct literal of type ...") past
        # the first clause so the signature stays stable across minor wording.
        head = re.split(r"\s+(?:in|at)\s+", head, maxsplit=1)[0]
        return f"{norm_file}::{head.lower()}"


@dataclass
class BuildCheckResult:
    """Outcome of one build verification pass.

    Four mutually-meaningful shapes:
      * ``ok=True``                  鈥?the command ran and reported no errors.
      * ``ok=False`` + ``errors``    鈥?the command ran and produced parseable
                                       compile/collection errors.
      * ``skipped=True``             鈥?no compile step for this build system
                                       (node / unknown); not a failure.
      * ``unverifiable=True``        鈥?the command could NOT be run (toolchain
                                       missing, rc=127) or exited non-zero with
                                       no parseable error.  This is NOT a pass:
                                       the gate has no opinion on the patch and
                                       the caller must not treat it as success.
    """

    system: BuildSystem
    ok: bool
    errors: list[BuildError] = field(default_factory=list)
    raw_output: str = ""
    command: str = ""
    skipped: bool = False
    timed_out: bool = False
    unverifiable: bool = False
    # Distinguishes the TWO causes of ``unverifiable``:
    #   * ``toolchain_missing=True``  鈥?the executable could not be spawned
    #     (rc=127). The gate genuinely has no opinion (e.g. no `go` on a
    #     Windows host); the caller may accept the patch unverified.
    #   * ``toolchain_missing=False`` (but ``unverifiable=True``) 鈥?a command
    #     that DID run exited non-zero yet produced no parseable error. This is
    #     a real failure we could not attribute, NOT a free pass: the caller
    #     must treat it conservatively as a build failure.
    toolchain_missing: bool = False

    def signatures(self) -> set[str]:
        return {e.signature() for e in self.errors}


# 鈹€鈹€ Parsers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

# Go compiler/vet error: ``path/to/file.go:line:col: message`` (col optional).
# ``go vet`` prefixes its diagnostics with a literal ``vet: `` (e.g.
# ``vet: lib/kube/proxy/forwarder_test.go:49:4: unknown field Client``); strip
# that optional prefix so vet errors are parsed identically to build errors 鈥?# otherwise a real vet failure parses to ZERO errors and the gate misreports
# the failure as ``unverifiable`` instead of ``BUILD_FAILED``.
_GO_ERROR_RE = re.compile(
    r"^(?:vet:\s+)?(?P<file>[^\s:]+\.go):(?P<line>\d+):(?:\d+:)?\s+(?P<msg>.+)$"
)

# pytest summary line: ``ERROR path/to/test_x.py`` possibly followed by a
# ``::nodeid`` and/or ``- SomeError: detail``.  We capture the leading file
# token (greedy up to the first whitespace) and strip any ``::nodeid`` suffix;
# the message is taken from a trailing `` - ...`` clause when present, else from
# the most recent ``E   ...`` exception line.
_PYTEST_SUMMARY_ERROR_RE = re.compile(r"^ERROR\s+(?P<tok>\S+\.py\S*)")
# pytest ``___ ERROR collecting path/to/test_x.py ___`` collection header.
_PYTEST_COLLECT_RE = re.compile(
    r"ERROR collecting\s+(?P<file>[^\s]+\.py)"
)
# pytest detailed exception line: ``E   AttributeError: detail``.
_PYTEST_EXC_RE = re.compile(r"^E\s+(?P<msg>\w+(?:Error|Exception|Warning):\s+.+)$")


def parse_go_errors(text: str) -> list[BuildError]:
    """Extract Go compile/vet errors from combined build output."""
    errors: list[BuildError] = []
    seen: set[tuple[str, int | None, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        m = _GO_ERROR_RE.match(stripped)
        if not m:
            continue
        file = m.group("file")
        line_no = int(m.group("line"))
        msg = m.group("msg").strip()
        key = (file, line_no, msg)
        if key in seen:
            continue
        seen.add(key)
        errors.append(BuildError(file=file, line=line_no, message=msg, raw=stripped))
    return errors


def parse_python_errors(text: str) -> list[BuildError]:
    """Extract pytest collection errors from ``--collect-only`` output.

    Strategy: the summary block lists each failing file via ``ERROR <path>``;
    the detailed traceback contributes the exception message via ``E   ...``.
    We pair the most recent exception message with each collection-header file
    when present, and always emit one record per summary ``ERROR <path>`` so a
    file with no parsed exception line still surfaces.
    """
    errors: list[BuildError] = []
    seen_files: set[str] = set()
    last_exc: str = ""
    collect_file: str | None = None

    for line in text.splitlines():
        stripped = line.strip()

        mc = _PYTEST_COLLECT_RE.search(stripped)
        if mc:
            collect_file = mc.group("file")
            continue

        me = _PYTEST_EXC_RE.match(stripped)
        if me:
            last_exc = me.group("msg").strip()
            if collect_file and collect_file not in seen_files:
                seen_files.add(collect_file)
                errors.append(
                    BuildError(
                        file=collect_file,
                        line=None,
                        message=last_exc,
                        raw=stripped,
                    )
                )
            continue

        ms = _PYTEST_SUMMARY_ERROR_RE.match(stripped)
        if ms:
            file = ms.group("tok").split("::", 1)[0]
            if file in seen_files:
                continue
            seen_files.add(file)
            # Prefer a trailing " - <msg>" clause; else the last E-line.
            dash_msg = ""
            if " - " in stripped:
                dash_msg = stripped.split(" - ", 1)[1].strip()
            msg = (dash_msg or last_exc or "collection error").strip()
            errors.append(
                BuildError(file=file, line=None, message=msg, raw=stripped)
            )

    return errors


# 鈹€鈹€ Runner 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _run(
    cmd: list[str],
    repo_dir: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str, bool]:
    """Run *cmd* in *repo_dir*; return (returncode, combined_output, timed_out)."""
    return run_repo_command(cmd, repo_dir=repo_dir, timeout=timeout, env=env)


def run_build_check(
    repo_dir: Path,
    system: BuildSystem | None = None,
    timeout: int = 1200,
    python_targets: list[str] | None = None,
    go_targets: list[str] | None = None,
) -> BuildCheckResult:
    """Compile / collect the patched tree and return structured errors.

    Does NOT mutate the working tree.  ``ok`` is True only when every command
    exits 0 and no errors were parsed.
    """
    repo_dir = Path(repo_dir)
    if system is None:
        system = detect_build_system(repo_dir)

    if system == "go":
        return _run_go(repo_dir, timeout, go_targets=go_targets)
    if system == "python":
        return _run_python(repo_dir, timeout, python_targets=python_targets)

    # node / unknown: do not block 鈥?log via skipped flag.
    return BuildCheckResult(
        system=system,
        ok=True,
        skipped=True,
        command="(skipped: no compile step for this build system)",
    )


def changed_go_packages(repo_dir: Path) -> list[str]:
    """Return package directory selectors for changed production Go files."""
    # The host worktree is canonical. Asking the Docker executor to discover
    # changed paths is circular: run_repo_command first syncs the patch and a
    # sync/reset anomaly can make the discovery command report an empty diff,
    # which silently skips compile. Only the actual toolchain command belongs
    # in the executor.
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--name-only",
             "--diff-filter=ACMRT", "HEAD", "--", "*.go"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    packages: set[str] = set()
    for raw in proc.stdout.splitlines():
        rel = raw.strip().replace("\\", "/")
        if not rel or rel.endswith("_test.go"):
            continue
        parent = str(Path(rel).parent).replace("\\", "/")
        packages.add("." if parent in {"", "."} else f"./{parent}")
    return sorted(packages)


def _run_go(
    repo_dir: Path,
    timeout: int,
    go_targets: list[str] | None = None,
) -> BuildCheckResult:
    """Compile affected Go packages plus package-local tests without running them."""
    targets = list(go_targets) if go_targets is not None else changed_go_packages(repo_dir)
    if not targets and not (Path(repo_dir) / ".git").exists() and any(Path(repo_dir).glob("*.go")):
        targets = ["."]
    if not targets:
        return BuildCheckResult(
            system="go",
            ok=True,
            skipped=True,
            command="(skipped: no changed production Go packages)",
        )
    commands = [["go", "build", *targets]]
    for idx, target in enumerate(targets, start=1):
        commands.append([
            "go", "test", "-c",
            "-o", f"/tmp/build-verify-{idx}.test",
            target,
        ])
    all_errors: list[BuildError] = []
    raw_parts: list[str] = []
    ok = True
    timed_out = False
    toolchain_missing = False
    seen_sig: set[str] = set()

    for cmd in commands:
        rc, out, t_out = _run(cmd, repo_dir, timeout)
        raw_parts.append(f"$ {' '.join(cmd)} (rc={rc})\n{out}")
        if t_out:
            timed_out = True
        if rc == _RC_TOOLCHAIN_MISSING:
            toolchain_missing = True
        if rc != 0:
            ok = False
        for err in parse_go_errors(out):
            if err.signature() in seen_sig:
                continue
            seen_sig.add(err.signature())
            all_errors.append(err)

    # Unverifiable when the toolchain could not be spawned, or a command failed
    # but produced no parseable error (a failure we cannot attribute to code).
    # A timeout is a distinct, honestly-reported condition 鈥?not unverifiable.
    unverifiable = (
        not timed_out
        and (toolchain_missing or (not ok and not all_errors))
    )

    return BuildCheckResult(
        system="go",
        ok=ok and not all_errors,
        errors=all_errors,
        raw_output="\n\n".join(raw_parts),
        command=" && ".join(" ".join(c) for c in commands),
        timed_out=timed_out,
        unverifiable=unverifiable,
        toolchain_missing=toolchain_missing,
    )


def changed_python_production_files(repo_dir: Path) -> list[str]:
    """Return changed Python production files, excluding tests/examples."""
    repo_dir = Path(repo_dir)
    paths: set[str] = set()
    commands = (
        ["diff", "--name-only", "--diff-filter=ACMRT", "HEAD", "--", "*.py"],
        ["ls-files", "--others", "--exclude-standard", "--", "*.py"],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_dir), *cmd],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        if proc.returncode != 0:
            continue
        for raw in proc.stdout.splitlines():
            rel = raw.strip().replace("\\", "/")
            if rel and _is_python_production_file(rel):
                paths.add(rel)
    return sorted(paths)


def _is_python_production_file(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if not parts or not rel.endswith(".py"):
        return False
    if parts[0] in {"test", "tests", "examples"}:
        return False
    if "test" in parts or "tests" in parts:
        return False
    return True


def _module_name_for_path(rel: str) -> str | None:
    path = rel.replace("\\", "/")
    if path.startswith("lib/"):
        path = path[len("lib/"):]
    if not path.endswith(".py"):
        return None
    path = path[:-3]
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    if not all(re.match(r"^[A-Za-z_]\w*$", part) for part in parts):
        return None
    return ".".join(parts)


def _python_env(repo_dir: Path) -> dict[str, str]:
    os_mod = __import__("os")
    env = dict(os_mod.environ)
    if docker_executor_enabled():
        root = executor_workdir().rstrip("/")
        roots = [f"{root}/lib", root]
    else:
        roots = [str(Path(repo_dir) / "lib"), str(Path(repo_dir))]
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os_mod.pathsep.join(roots + ([current] if current else []))
    return env


def _last_python_exception(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if re.match(r"^\w+(?:Error|Exception|Warning):\s+.+", stripped):
            return stripped
    return "python import/compile failed"


def _last_python_line(text: str) -> int | None:
    matches = re.findall(r'File "[^"]+", line (\d+)', text)
    return int(matches[-1]) if matches else None


def _run_python(
    repo_dir: Path,
    timeout: int,
    python_targets: list[str] | None = None,
) -> BuildCheckResult:
    """Compile/import changed production modules without reading tests."""
    targets = list(python_targets) if python_targets is not None else changed_python_production_files(repo_dir)
    if not targets:
        return BuildCheckResult(
            system="python",
            ok=True,
            skipped=True,
            command="(skipped: no changed production Python files)",
        )

    errors: list[BuildError] = []
    raw_parts: list[str] = []
    timed_out = False
    toolchain_missing = False
    env = _python_env(repo_dir)

    for rel in targets:
        compile_cmd = [
            "python",
            "-c",
            "import py_compile, sys; py_compile.compile(sys.argv[1], doraise=True)",
            rel,
        ]
        rc, out, t_out = _run(compile_cmd, repo_dir, timeout, env=env)
        raw_parts.append(f"$ {' '.join(compile_cmd)} (rc={rc})\n{out}")
        timed_out = timed_out or t_out
        toolchain_missing = toolchain_missing or rc == _RC_TOOLCHAIN_MISSING
        if rc != 0:
            errors.append(BuildError(file=rel, line=_last_python_line(out), message=_last_python_exception(out), raw=out))
            continue

        module = _module_name_for_path(rel)
        if not module:
            continue
        if rel.replace("\\", "/").startswith("scripts/"):
            import_cmd = [
                "python",
                "-c",
                (
                    "import importlib.util, pathlib, sys; "
                    "p=pathlib.Path(sys.argv[1]).resolve(); "
                    "sys.path.insert(0, str(p.parent)); "
                    "spec=importlib.util.spec_from_file_location('__harness_script__', p); "
                    "m=importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(m)"
                ),
                rel,
            ]
        else:
            import_cmd = [
                "python",
                "-c",
                "import importlib, sys; importlib.import_module(sys.argv[1])",
                module,
            ]
        rc, out, t_out = _run(import_cmd, repo_dir, timeout, env=env)
        raw_parts.append(f"$ {' '.join(import_cmd)} (rc={rc})\n{out}")
        timed_out = timed_out or t_out
        toolchain_missing = toolchain_missing or rc == _RC_TOOLCHAIN_MISSING
        if rc != 0:
            errors.append(BuildError(file=rel, line=_last_python_line(out), message=_last_python_exception(out), raw=out))

    ok = not errors and not toolchain_missing and not timed_out
    unverifiable = toolchain_missing and not timed_out
    return BuildCheckResult(
        system="python",
        ok=ok,
        errors=errors,
        raw_output="\n\n".join(raw_parts),
        command="python production compile/import: " + ", ".join(targets),
        timed_out=timed_out,
        unverifiable=unverifiable,
        toolchain_missing=toolchain_missing,
    )

# 鈹€鈹€ Baseline diff 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def diff_new_errors(
    baseline: BuildCheckResult | None,
    post: BuildCheckResult,
) -> list[BuildError]:
    """Return post-patch errors absent from the baseline (by signature).

    When *baseline* is None (baseline could not be computed), every post error
    is treated as new 鈥?the conservative assumption that ``base_commit`` is a
    real, compiling commit.
    """
    if baseline is None:
        return list(post.errors)
    base_sigs = baseline.signatures()
    return [e for e in post.errors if e.signature() not in base_sigs]


def render_errors_for_feedback(errors: list[BuildError], limit: int = 40) -> str:
    """Render new build errors as a compact text block for planner feedback."""
    if not errors:
        return ""
    lines: list[str] = []
    for err in errors[:limit]:
        loc = f"{err.file}:{err.line}" if err.line is not None else err.file
        lines.append(f"- {loc}: {err.message}")
    if len(errors) > limit:
        lines.append(f"- ... and {len(errors) - limit} more")
    return "\n".join(lines)

