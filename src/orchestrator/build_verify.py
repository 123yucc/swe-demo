"""
Post-patch build verification (code-driven, no LLM).

After the patch-generator applies SEARCH/REPLACE edits, the working tree may
*apply cleanly* yet still be broken: a renamed Go struct field left stale in a
sibling file, a config schema that references a type that was never defined,
an unexported method that base-commit tests still call.  Nothing in the patch
pipeline caught this — the generator only verifies that the edited file shows
up in ``git diff``.

This module is the deterministic backstop.  It compiles / collects the patched
tree in ``repo_dir`` (which is ``/app`` inside the SWE-bench docker image, so
the toolchain and dependencies are present) and reports compile/collection
errors as structured ``BuildError`` records.

IMPORTANT — methodological boundary: this NEVER runs ``before_repo_set_cmd``
and NEVER pulls in the hidden gold test files.  It only compiles the patched
production code plus the test files that already exist at ``base_commit``,
which the agent legitimately has access to.

Language coverage:
  * go      → ``go build ./...`` + ``go vet ./...`` (vet also compiles _test.go)
  * python  → ``python -m pytest --collect-only -q`` (collection imports test
              modules + conftest, triggering config-load / import errors)
  * node    → skipped (plain JS has no compile step; observed failures are all
              Go/Python — do not block, just log)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BuildSystem = Literal["go", "python", "node", "java", "unknown"]

# Return code from ``_run`` when the toolchain executable itself was not found
# (FileNotFoundError / OSError on subprocess spawn). Distinct from a non-zero
# rc produced by a toolchain that *did* run and rejected the code.
_RC_TOOLCHAIN_MISSING = 127


# ── Detection ──────────────────────────────────────────────────────────────

def detect_build_system(repo_dir: Path) -> BuildSystem:
    """Classify the repo's build system by marker files.

    Precedence: go.mod > python (pyproject/setup.py/setup.cfg) > java
    (pom.xml/build.gradle) > package.json.  Go takes precedence because a Go
    repo's static compile is the highest-value check; Python next; Java's
    marker is checked before ``package.json`` because a JVM repo with a
    ``package.json`` for front-end assets is still a JVM repo. ``package.json``
    only when no compiled-toolchain marker is present.

    NOTE — java is recognised here only so the phase-26 dynamic-grounding gate
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


# ── Structured errors ──────────────────────────────────────────────────────

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
      * ``ok=True``                  — the command ran and reported no errors.
      * ``ok=False`` + ``errors``    — the command ran and produced parseable
                                       compile/collection errors.
      * ``skipped=True``             — no compile step for this build system
                                       (node / unknown); not a failure.
      * ``unverifiable=True``        — the command could NOT be run (toolchain
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
    #   * ``toolchain_missing=True``  — the executable could not be spawned
    #     (rc=127). The gate genuinely has no opinion (e.g. no `go` on a
    #     Windows host); the caller may accept the patch unverified.
    #   * ``toolchain_missing=False`` (but ``unverifiable=True``) — a command
    #     that DID run exited non-zero yet produced no parseable error. This is
    #     a real failure we could not attribute, NOT a free pass: the caller
    #     must treat it conservatively as a build failure.
    toolchain_missing: bool = False

    def signatures(self) -> set[str]:
        return {e.signature() for e in self.errors}


# ── Parsers ────────────────────────────────────────────────────────────────

# Go compiler/vet error: ``path/to/file.go:line:col: message`` (col optional).
# ``go vet`` prefixes its diagnostics with a literal ``vet: `` (e.g.
# ``vet: lib/kube/proxy/forwarder_test.go:49:4: unknown field Client``); strip
# that optional prefix so vet errors are parsed identically to build errors —
# otherwise a real vet failure parses to ZERO errors and the gate misreports
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


# ── Runner ─────────────────────────────────────────────────────────────────

def _run(cmd: list[str], repo_dir: Path, timeout: int) -> tuple[int, str, bool]:
    """Run *cmd* in *repo_dir*; return (returncode, combined_output, timed_out)."""
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
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return 124, out, True
    except (FileNotFoundError, OSError) as exc:
        return 127, f"{type(exc).__name__}: {exc}", False
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False


def run_build_check(
    repo_dir: Path,
    system: BuildSystem | None = None,
    timeout: int = 1200,
) -> BuildCheckResult:
    """Compile / collect the patched tree and return structured errors.

    Does NOT mutate the working tree.  ``ok`` is True only when every command
    exits 0 and no errors were parsed.
    """
    repo_dir = Path(repo_dir)
    if system is None:
        system = detect_build_system(repo_dir)

    if system == "go":
        return _run_go(repo_dir, timeout)
    if system == "python":
        return _run_python(repo_dir, timeout)

    # node / unknown: do not block — log via skipped flag.
    return BuildCheckResult(
        system=system,
        ok=True,
        skipped=True,
        command="(skipped: no compile step for this build system)",
    )


def _run_go(repo_dir: Path, timeout: int) -> BuildCheckResult:
    """``go build ./...`` then ``go vet ./...``; merge parsed errors."""
    commands = [["go", "build", "./..."], ["go", "vet", "./..."]]
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
    # A timeout is a distinct, honestly-reported condition — not unverifiable.
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


def _run_python(repo_dir: Path, timeout: int) -> BuildCheckResult:
    """``python -m pytest --collect-only -q`` — collection imports modules."""
    cmd = ["python", "-m", "pytest", "--collect-only", "-q"]
    rc, out, t_out = _run(cmd, repo_dir, timeout)
    errors = parse_python_errors(out)
    # Only a missing interpreter is unverifiable here. pytest has benign
    # non-zero exits (rc=5 = no tests collected, rc=4 = usage) that must NOT
    # be misread as unverifiable; genuine collection failures always emit
    # parseable ``ERROR`` lines, which land in ``errors`` above.
    unverifiable = (rc == _RC_TOOLCHAIN_MISSING) and not t_out
    return BuildCheckResult(
        system="python",
        ok=(rc == 0) and not errors,
        errors=errors,
        raw_output=f"$ {' '.join(cmd)} (rc={rc})\n{out}",
        command=" ".join(cmd),
        timed_out=t_out,
        unverifiable=unverifiable,
        toolchain_missing=(rc == _RC_TOOLCHAIN_MISSING),
    )


# ── Baseline diff ──────────────────────────────────────────────────────────

def diff_new_errors(
    baseline: BuildCheckResult | None,
    post: BuildCheckResult,
) -> list[BuildError]:
    """Return post-patch errors absent from the baseline (by signature).

    When *baseline* is None (baseline could not be computed), every post error
    is treated as new — the conservative assumption that ``base_commit`` is a
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
