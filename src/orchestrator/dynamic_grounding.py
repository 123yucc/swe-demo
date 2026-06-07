"""
Dynamic evidence grounding (phase 26 — Correct Attribution, ③, runtime layer).

``grounding.py`` (static) proves a cited location *exists*; ``ast_grounding.py``
proves it is *structurally reachable*. This module proves the strongest thing of
all WITHOUT inventing an acceptance criterion: that a cited location is on the
ACTUAL FAILURE PATH of the bug, by reproducing the bug once on the
``base_commit`` tree and comparing the observed execution path (stack frames ∪
covered lines) against the cited regions.

Methodological boundary (see docs/plan/phase26_dynamic_grounding.md):
  * We only TRIGGER and OBSERVE buggy behaviour. We NEVER author a test that
    asserts "what the fix should produce" — that would invent an acceptance
    criterion, which is hallucination.
  * A restricted LLM is allowed for ONE thing only: translating the
    problem_statement's reproduction steps into an executable trigger script.
    It never judges grounding, never defines correct behaviour, never asserts.
  * The grounding judgement is always script-execution + mechanical comparison.

The single most important discriminator is the SYMPTOM GATE (``observed_symptom``):
the test that exercises the bug is the evaluator's hidden gold test
(``fail_to_pass``), absent at base_commit. So base_commit's *existing* tests
nearly all PASS on buggy code — driving one of them and seeing coverage hit a
cited line only re-proves reachability (which AST already gives), NOT root cause.
A strong ``dynamic_reached`` therefore requires the run to actually EXHIBIT the
symptom (exception / panic / failure stack comparable to the symptom card).
"Executed but no symptom" is downgraded to ``dynamic_unverifiable_fallback`` —
coverage hits never masquerade as a symptom.

Three states (inherited from ``build_verify``'s lesson):
  * ``dynamic_reached``              — symptom reproduced AND path hit a cited
                                       location. Positive confidence signal.
  * ``dynamic_not_reached``          — symptom reproduced but path missed all
                                       cited locations. SOFT signal for the LLM
                                       questioner; NEVER auto-resets (repro
                                       scripts are legitimately incomplete).
  * ``dynamic_unverifiable_fallback``— script would not run / ran with NO
                                       symptom / toolchain missing (rc=127) /
                                       setup error / no symptom-capable source.
                                       No opinion; static results stand.

Offline-docker constraint: the harness runs inside each instance's SWE-bench
image (``--repo-dir /app``). Each image ships exactly ONE language toolchain and
has NO network. So every adapter uses only that language's native runner and
never installs anything. Stack/exception parsing is the unconditional primary
signal (zero deps, four languages); coverage is an opportunistic enhancement
used only when ``probe_coverage`` finds the tool already present.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from src.models.context import EvidenceCards
from src.orchestrator.audit import _parse_evidence_location
from src.orchestrator.ast_grounding import (
    build_symbol_index,
    def_spans_containing,
)
from src.orchestrator.build_verify import BuildSystem, detect_build_system


# Return code marking a toolchain executable that could not be spawned
# (FileNotFoundError / OSError), identical convention to build_verify.
_RC_TOOLCHAIN_MISSING = 127

GroundedBy = Literal[
    "dynamic_reached",
    "dynamic_not_reached",
    "dynamic_unverifiable_fallback",
]


# ── Result types ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunResult:
    """Raw outcome of one reproduction subprocess run.

    ``returncode`` is the process exit code (124 = timeout, 127 = toolchain
    missing). ``trace`` is the parsed stack/exception frames ``[(file, line)]``;
    ``coverage`` is the parsed line-hit set ``{file: {lines}}`` (empty when
    coverage was unavailable — that is NOT an error, just trace-only). ``error``
    holds a setup/spawn problem string when the run could not be evaluated.
    """

    returncode: int
    output: str
    trace: list[tuple[str, int]] = field(default_factory=list)
    coverage: dict[str, set[int]] = field(default_factory=dict)
    exception_text: str = ""
    timed_out: bool = False
    toolchain_missing: bool = False
    setup_error: str = ""


@dataclass(frozen=True)
class DynamicGroundingResult:
    """Per-requirement (or global) dynamic grounding outcome.

    ``grounded_by`` is the three-state tag. ``requirement_id`` is the cited
    requirement this result speaks to, or ``"<global>"`` for a case-level
    outcome (e.g. unverifiable before any per-req matching could run).
    ``reached_locations`` / ``missed_locations`` list the cited regions the
    run did / did not traverse. ``observed`` records whether the symptom gate
    fired. ``detail`` is a one-line human summary for logs / LLM note.
    """

    requirement_id: str
    grounded_by: GroundedBy
    observed: bool = False
    reached_locations: list[str] = field(default_factory=list)
    missed_locations: list[str] = field(default_factory=list)
    exception_text: str = ""
    detail: str = ""

    def render(self) -> str:
        return f"{self.requirement_id}: [{self.grounded_by}] {self.detail}"


# ── Reproduction source ─────────────────────────────────────────────────────

ReproBackend = Literal["existing_test_template", "synthetic_script", "known_bug_test"]


@dataclass(frozen=True)
class ReproductionSource:
    """A concrete, runnable reproduction artifact.

    ``backend`` records how it was produced; ``language`` selects the adapter;
    ``script_relpath`` is the repo-relative temp file written for the run;
    ``run_target`` is an optional native-runner selector (e.g. a ``go test
    -run`` pattern or ``pytest`` nodeid). ``created_paths`` are temp files to
    delete after the run.
    """

    backend: ReproBackend
    language: BuildSystem
    script_relpath: str
    run_target: str = ""
    created_paths: list[str] = field(default_factory=list)


# ── Language adapter registry ───────────────────────────────────────────────

@dataclass(frozen=True)
class LangAdapter:
    """One language's reproduction backend.

    Hooks (all pure, no LLM):
      * ``detect(repo_dir) -> bool``         — does this adapter own the repo?
      * ``drive_cmd(source) -> list[str]``   — argv to run the reproduction.
      * ``parse_trace(output) -> [(f,l)]``   — stack/exception frame parser.
      * ``probe_coverage(repo_dir) -> bool`` — is a coverage tool already present?
      * ``parse_coverage(repo_dir, output) -> {file:{lines}}`` — coverage parser.
      * ``script_suffix``                    — temp script extension.
    """

    name: BuildSystem
    detect: Callable[[Path], bool]
    drive_cmd: Callable[[ReproductionSource], list[str]]
    parse_trace: Callable[[str], list[tuple[str, int]]]
    probe_coverage: Callable[[Path], bool]
    parse_coverage: Callable[[Path, str], dict[str, set[int]]]
    script_suffix: str


# ── Trace parsers (primary signal — zero-dependency, four languages) ─────────

# Python traceback: ``  File "path/to/x.py", line 42, in fn``
_PY_TB_RE = re.compile(r'^\s*File "(?P<file>[^"]+\.py)",\s+line\s+(?P<line>\d+)')
# Python final exception line: ``ValueError: detail`` (no leading File).
_PY_EXC_RE = re.compile(r"^(?P<exc>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning))\b.*$")

# Go panic / goroutine stack frame: ``\tpath/to/file.go:123 +0x1f``
_GO_FRAME_RE = re.compile(r"^\s*(?P<file>[^\s:]+\.go):(?P<line>\d+)(?:\s|$|\s\+0x)")
_GO_PANIC_RE = re.compile(r"^panic:\s*(?P<msg>.+)$")

# Java stack frame: ``\tat pkg.Cls.method(File.java:123)``
_JAVA_FRAME_RE = re.compile(r"^\s*at\s+[\w.$]+\((?P<file>[^():]+\.java):(?P<line>\d+)\)")
_JAVA_EXC_RE = re.compile(r"^(?:Caused by:\s*)?(?P<exc>[\w.$]*(?:Exception|Error))\b.*$")

# JS error stack frame: ``    at fn (path/to/file.js:12:34)`` or ``at file.js:12:34``
_JS_FRAME_RE = re.compile(
    r"^\s*at\s+(?:.*?\()?(?P<file>[^()\s]+\.(?:js|mjs|cjs|ts|jsx|tsx)):(?P<line>\d+):\d+\)?"
)
_JS_EXC_RE = re.compile(r"^(?:Uncaught\s+)?(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception))\b.*$")


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def parse_python_trace(output: str) -> list[tuple[str, int]]:
    frames: list[tuple[str, int]] = []
    for line in output.splitlines():
        m = _PY_TB_RE.match(line)
        if m:
            frames.append((_norm(m.group("file")), int(m.group("line"))))
    return frames


def parse_go_trace(output: str) -> list[tuple[str, int]]:
    frames: list[tuple[str, int]] = []
    for line in output.splitlines():
        m = _GO_FRAME_RE.match(line)
        if m:
            frames.append((_norm(m.group("file")), int(m.group("line"))))
    return frames


def parse_java_trace(output: str) -> list[tuple[str, int]]:
    frames: list[tuple[str, int]] = []
    for line in output.splitlines():
        m = _JAVA_FRAME_RE.match(line)
        if m:
            # Java frames carry only the bare File.java name, not a repo path.
            frames.append((_norm(m.group("file")), int(m.group("line"))))
    return frames


def parse_js_trace(output: str) -> list[tuple[str, int]]:
    frames: list[tuple[str, int]] = []
    for line in output.splitlines():
        m = _JS_FRAME_RE.match(line)
        if m:
            frames.append((_norm(m.group("file")), int(m.group("line"))))
    return frames


def _extract_exception(output: str, lang: BuildSystem) -> str:
    """Best-effort: the most informative exception/panic line for the symptom gate."""
    exc_re = {
        "python": _PY_EXC_RE,
        "go": _GO_PANIC_RE,
        "java": _JAVA_EXC_RE,
        "node": _JS_EXC_RE,
    }.get(lang)
    found = ""
    for line in output.splitlines():
        stripped = line.strip()
        if lang == "go":
            m = _GO_PANIC_RE.match(stripped)
            if m:
                return f"panic: {m.group('msg').strip()}"
            continue
        if exc_re is not None:
            m = exc_re.match(stripped)
            if m:
                # Keep the last (outermost) exception line — for chained
                # exceptions the final summary is the most descriptive.
                found = stripped
    return found


# ── Coverage probes + parsers (opportunistic enhancement) ────────────────────

def _probe_none(_repo_dir: Path) -> bool:
    return False


def _probe_go_coverage(_repo_dir: Path) -> bool:
    # ``-coverprofile`` is built into ``go test`` — stable whenever go is present.
    return shutil.which("go") is not None


def _probe_python_coverage(repo_dir: Path) -> bool:
    # coverage.py must already be importable in the repo's environment; we never
    # install it (offline). Probe by importability.
    try:
        import coverage  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _parse_coverage_py(repo_dir: Path, _output: str) -> dict[str, set[int]]:
    """Parse a ``.coverage`` SQLite db written into repo_dir, if present."""
    cov_file = repo_dir / ".coverage"
    if not cov_file.is_file():
        return {}
    try:
        from coverage import CoverageData  # type: ignore
    except Exception:
        return {}
    data = CoverageData(basename=str(cov_file))
    try:
        data.read()
    except Exception:
        return {}
    out: dict[str, set[int]] = {}
    for measured in data.measured_files():
        lines = data.lines(measured)
        if not lines:
            continue
        try:
            rel = _norm(str(Path(measured).relative_to(repo_dir)))
        except ValueError:
            rel = _norm(measured)
        out.setdefault(rel, set()).update(lines)
    return out


def _parse_go_coverage(repo_dir: Path, _output: str) -> dict[str, set[int]]:
    """Parse a Go ``-coverprofile`` file (``coverage.out``) into hit lines.

    Format: ``name.go:startLine.col,endLine.col numStmts count``; count>0 = hit.
    """
    prof = repo_dir / "coverage.out"
    if not prof.is_file():
        return {}
    out: dict[str, set[int]] = {}
    try:
        text = prof.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        if line.startswith("mode:"):
            continue
        m = re.match(
            r"^(?P<file>\S+\.go):(?P<s>\d+)\.\d+,(?P<e>\d+)\.\d+\s+\d+\s+(?P<n>\d+)$",
            line,
        )
        if not m or int(m.group("n")) == 0:
            continue
        # Go coverprofile file token includes the module path prefix; keep the
        # basename-ish tail so it matches repo-relative cited paths heuristically.
        fname = _norm(m.group("file"))
        for ln in range(int(m.group("s")), int(m.group("e")) + 1):
            out.setdefault(fname, set()).add(ln)
    return out


# ── Symptom gate ─────────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "is", "are", "be", "to", "of", "in", "on", "for", "and",
    "or", "with", "this", "that", "it", "as", "at", "by", "error", "exception",
    "should", "must", "when", "if", "raises", "raise", "throws", "thrown",
    "fails", "failure", "returns", "value", "values",
}


def _tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or "")
        if len(t) >= 3 and t.lower() not in _STOPWORDS
    }


def observed_symptom(run: RunResult, symptom_card) -> bool:
    """Symptom gate — the ONLY precondition for a strong ``dynamic_reached``.

    True only when the run actually exhibited the symptom card's observable
    failure: a non-clean termination PLUS an exception/panic/failure whose text
    is comparable to the symptom card. A silently-passing run (rc==0, no
    exception) is NEVER a symptom — coverage hits on such a run do not count.

    Comparability is deliberately lenient (the bug is real; we only guard
    against "no failure at all"): any non-zero exit with a parsed exception, OR
    a parsed exception whose tokens overlap the symptom card, qualifies. A
    timeout / toolchain-missing / setup error is NOT a symptom.
    """
    if run.toolchain_missing or run.timed_out or run.setup_error:
        return False
    if run.returncode == 0 and not run.exception_text:
        # Clean pass — reachability only, not a symptom.
        return False
    if not run.exception_text and not run.trace:
        # Non-zero exit but nothing parseable — cannot attribute to a symptom.
        return False

    # We have a failure with at least an exception line or a stack. That alone
    # is a reproduced symptom (the run was crafted to trigger the bug). If the
    # symptom card names specific tokens, a soft overlap strengthens the match
    # but its absence does not veto — the failing run IS the symptom.
    card_tokens: set[str] = set()
    for line in getattr(symptom_card, "observable_failures", []) or []:
        card_tokens |= _tokens(line)
    if not card_tokens:
        return True
    run_tokens = _tokens(run.exception_text) | _tokens(run.output[-2000:])
    # Non-empty overlap OR the exception type itself is a strong enough signal.
    return bool(card_tokens & run_tokens) or bool(run.exception_text)


# ── Path matching (language-agnostic; called ONLY when symptom observed) ──────

def _cited_paths_lines(cited_regions: list[str]) -> dict[str, set[int]]:
    """Expand cited ``path:LINE[-LINE]`` regions into ``{path: {lines}}``."""
    out: dict[str, set[int]] = {}
    for region in cited_regions:
        path, start, end = _parse_evidence_location(region)
        if not path or start == 0:
            continue
        if end is None:
            end = start
        out.setdefault(_norm(path), set()).update(range(start, end + 1))
    return out


def _path_tail_match(observed_path: str, cited_path: str) -> bool:
    """Match observed vs cited paths tolerantly by common suffix.

    Stack frames (esp. Go/Java) often carry absolute or module-prefixed paths,
    while cited regions are repo-relative. A shared trailing path segment chain
    (or shared basename for Java's bare ``File.java``) counts as the same file.
    """
    o = _norm(observed_path)
    c = _norm(cited_path)
    if o == c:
        return True
    if o.endswith("/" + c) or c.endswith("/" + o):
        return True
    # Java frames are basename-only.
    return os.path.basename(o) == os.path.basename(c) and bool(os.path.basename(o))


def match_path_reached(
    trace: list[tuple[str, int]],
    coverage: dict[str, set[int]],
    cited_regions: list[str],
    repo_dir: Path,
) -> tuple[list[str], list[str]]:
    """Return ``(reached, missed)`` cited regions vs the observed path.

    A cited region is REACHED when the observed path (stack frames ∪ covered
    lines) touches any of its lines, OR touches any line inside the AST def-span
    that contains a cited line (function-granularity, robust to line drift).
    Pure mechanical comparison — never called unless the symptom gate fired.
    """
    cited = _cited_paths_lines(cited_regions)
    if not cited:
        return [], []

    # Observed line hits per path: trace frames + coverage.
    observed: dict[str, set[int]] = {}
    for f, ln in trace:
        observed.setdefault(_norm(f), set()).add(ln)
    for f, lines in coverage.items():
        observed.setdefault(_norm(f), set()).update(lines)

    # Expand each cited region to its enclosing function span (per file, via AST).
    def_spans: dict[str, list[tuple[int, int]]] = {}
    for cited_path, lines in cited.items():
        fp = repo_dir / cited_path
        if not fp.is_file():
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = build_symbol_index(cited_path, content)
        if idx is None:
            continue
        spans: list[tuple[int, int]] = []
        for ln in lines:
            for d in def_spans_containing(idx, ln):
                spans.append((d.lineno, d.end_lineno))
        if spans:
            def_spans[cited_path] = spans

    reached: list[str] = []
    missed: list[str] = []
    for region in cited_regions:
        path, start, end = _parse_evidence_location(region)
        if not path or start == 0:
            continue
        if end is None:
            end = start
        cited_path = _norm(path)
        region_lines = set(range(start, end + 1))
        spans = def_spans.get(cited_path, [])

        hit = False
        for obs_path, obs_lines in observed.items():
            if not _path_tail_match(obs_path, cited_path):
                continue
            if obs_lines & region_lines:
                hit = True
                break
            # Function-granularity: an observed line inside the cited region's
            # enclosing def span counts as reaching it.
            if any(s <= ol <= e for (s, e) in spans for ol in obs_lines):
                hit = True
                break
        (reached if hit else missed).append(region)

    return reached, missed


# ── Drive commands per language ──────────────────────────────────────────────

def _drive_python(source: ReproductionSource) -> list[str]:
    return ["python", source.script_relpath]


def _drive_go(source: ReproductionSource) -> list[str]:
    # existing_test_template → run a single test by name; synthetic → run file.
    if source.backend == "existing_test_template" and source.run_target:
        return ["go", "test", "-run", source.run_target, "-count=1", "./..."]
    return ["go", "run", source.script_relpath]


def _drive_java(source: ReproductionSource) -> list[str]:
    if source.run_target:
        # Maven single-test selector: -Dtest=Cls#method
        return ["mvn", "-q", "-Dtest=" + source.run_target, "test"]
    # Bare java source run (JDK 11+ single-file mode).
    return ["java", source.script_relpath]


def _drive_js(source: ReproductionSource) -> list[str]:
    return ["node", source.script_relpath]


# ── Go drive with coverprofile when probed ───────────────────────────────────

def _drive_go_test_with_cov(source: ReproductionSource) -> list[str]:
    if source.backend == "existing_test_template" and source.run_target:
        return [
            "go", "test", "-run", source.run_target, "-count=1",
            "-coverprofile=coverage.out", "./...",
        ]
    return ["go", "run", source.script_relpath]


# ── Adapter registry ──────────────────────────────────────────────────────────

def _detect_python(repo_dir: Path) -> bool:
    return detect_build_system(repo_dir) == "python"


def _detect_go(repo_dir: Path) -> bool:
    return detect_build_system(repo_dir) == "go"


def _detect_java(repo_dir: Path) -> bool:
    return detect_build_system(repo_dir) == "java"


def _detect_node(repo_dir: Path) -> bool:
    return detect_build_system(repo_dir) == "node"


LANG_ADAPTERS: dict[BuildSystem, LangAdapter] = {
    "python": LangAdapter(
        name="python",
        detect=_detect_python,
        drive_cmd=_drive_python,
        parse_trace=parse_python_trace,
        probe_coverage=_probe_python_coverage,
        parse_coverage=_parse_coverage_py,
        script_suffix=".py",
    ),
    "go": LangAdapter(
        name="go",
        detect=_detect_go,
        drive_cmd=_drive_go_test_with_cov,
        parse_trace=parse_go_trace,
        probe_coverage=_probe_go_coverage,
        parse_coverage=_parse_go_coverage,
        script_suffix=".go",
    ),
    "java": LangAdapter(
        name="java",
        detect=_detect_java,
        drive_cmd=_drive_java,
        parse_trace=parse_java_trace,
        probe_coverage=_probe_none,  # JaCoCo needs build-configured agent; usually absent
        parse_coverage=lambda _d, _o: {},
        script_suffix=".java",
    ),
    "node": LangAdapter(
        name="node",
        detect=_detect_node,
        drive_cmd=_drive_js,
        parse_trace=parse_js_trace,
        probe_coverage=_probe_none,  # runner-dependent; trace-only by default
        parse_coverage=lambda _d, _o: {},
        script_suffix=".js",
    ),
}


def adapter_for(repo_dir: Path) -> LangAdapter | None:
    """Return the language adapter for *repo_dir*, or None (unknown → skip)."""
    system = detect_build_system(repo_dir)
    return LANG_ADAPTERS.get(system)


# ── Runner (subprocess + three-state, mirrors build_verify) ──────────────────

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
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out, True
    except (FileNotFoundError, OSError) as exc:
        return _RC_TOOLCHAIN_MISSING, f"{type(exc).__name__}: {exc}", False
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False


def run_reproduction(
    repo_dir: Path,
    source: ReproductionSource,
    adapter: LangAdapter,
    timeout: int = 300,
) -> RunResult:
    """Execute one reproduction and parse the observed path (no LLM, no judging).

    Captures stack/exception frames (primary) plus coverage (only when
    ``probe_coverage`` succeeds). Inherits build_verify's three-state lesson:
    rc=127 → toolchain missing; setup/spawn problems become ``setup_error``.
    Missing coverage is NOT an error — the run falls back to trace-only.
    """
    repo_dir = Path(repo_dir)
    use_coverage = False
    try:
        use_coverage = adapter.probe_coverage(repo_dir)
    except Exception:
        use_coverage = False

    cmd = adapter.drive_cmd(source)
    rc, output, timed_out = _run(cmd, repo_dir, timeout)

    trace = adapter.parse_trace(output)
    exception_text = _extract_exception(output, adapter.name)
    coverage: dict[str, set[int]] = {}
    if use_coverage and not timed_out and rc != _RC_TOOLCHAIN_MISSING:
        try:
            coverage = adapter.parse_coverage(repo_dir, output)
        except Exception:
            coverage = {}

    return RunResult(
        returncode=rc,
        output=output,
        trace=trace,
        coverage=coverage,
        exception_text=exception_text,
        timed_out=timed_out,
        toolchain_missing=(rc == _RC_TOOLCHAIN_MISSING),
    )


# ── Working-tree restore (base_commit hygiene) ───────────────────────────────

def _git(repo_dir: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def restore_worktree(repo_dir: Path, created_paths: list[str]) -> None:
    """Restore the base_commit working tree and delete any temp artifacts.

    Dynamic grounding runs at the EVIDENCE stage (pre-patch) on base_commit, so
    a reproduction script / coverage product must never leak into the tree the
    rest of the pipeline sees. Deletes created temp files explicitly, drops
    coverage products, then ``git checkout -- . && git clean -fd``.
    """
    repo_dir = Path(repo_dir)
    for rel in created_paths:
        try:
            (repo_dir / rel).unlink(missing_ok=True)
        except OSError:
            pass
    for product in (".coverage", "coverage.out", "coverage-final.json", "jacoco.exec"):
        try:
            (repo_dir / product).unlink(missing_ok=True)
        except OSError:
            pass
    _git(repo_dir, "checkout", "--", ".")
    _git(repo_dir, "clean", "-fd")


# ── Reproduction source selection ────────────────────────────────────────────

_TEST_FILE_GLOBS: dict[BuildSystem, tuple[str, ...]] = {
    "go": ("*_test.go",),
    "java": ("*Test.java", "*Tests.java"),
    "node": ("*.test.js", "*.test.ts", "*.spec.js"),
    "python": ("test_*.py", "*_test.py"),
}


def _suspect_dirs(evidence: EvidenceCards, repo_dir: Path) -> list[Path]:
    """Directories of cited suspect entities / regions — where templates live."""
    dirs: list[Path] = []
    seen: set[str] = set()
    candidates: list[str] = []
    for req in evidence.requirements:
        loc = req.scoped_evidence.localization
        candidates += loc.suspect_entities + loc.exact_code_regions
        candidates += req.evidence_locations
    for entry in candidates:
        path, _, _ = _parse_evidence_location(entry)
        if not path:
            continue
        d = str((repo_dir / path).parent)
        if d not in seen:
            seen.add(d)
            dirs.append(Path(d))
    return dirs


def find_template_test(
    evidence: EvidenceCards,
    repo_dir: Path,
    language: BuildSystem,
) -> Path | None:
    """Find an existing test file in a cited suspect directory (go/java/js first).

    Returns the nearest existing test file to a cited location, used by the
    ``existing_test_template`` backend to copy + retarget. None when none found.
    """
    globs = _TEST_FILE_GLOBS.get(language, ())
    if not globs:
        return None
    for d in _suspect_dirs(evidence, repo_dir):
        if not d.is_dir():
            continue
        for pat in globs:
            for fp in sorted(d.glob(pat)):
                if fp.is_file():
                    return fp
    return None


# ── Restricted-LLM script synthesis ──────────────────────────────────────────

_SYNTH_SYSTEM_PROMPT = """\
You translate a bug's reproduction steps into ONE executable trigger script.
You are a TRANSLATOR, not a judge. Hard rules — violating any is a failure:

  * Your script's ONLY purpose is to TRIGGER the buggy behaviour and let it
    surface naturally (raise the exception / panic / fail). OBSERVE only.
  * NEVER assert what the CORRECT or FIXED behaviour should be. No expected
    values, no `assert fixed == ...`, no "should equal". You do not know and
    must not invent the acceptance criterion.
  * NEVER import, reference, or recreate a hidden/gold test (e.g. the
    evaluator's fail_to_pass test). Use only the repository's existing public
    code paths.
  * When given an existing test function as a template, copy its structure and
    ONLY change the INPUT parameters to the values from the reproduction steps.
    Do not add assertions about the output.
  * If you cannot produce a runnable trigger from the given information, set
    `feasible=false` and leave `script` empty. Do not guess wildly.

The script will be run by the project's native runner on the buggy code. A good
script reaches the suspect code with the triggering input and lets the real
error propagate to stdout/stderr.
"""


def _synth_user_prompt(
    problem_statement: str,
    symptom_card,
    language: BuildSystem,
    suspect_entities: list[str],
    template_text: str | None,
    template_path: str | None,
) -> str:
    failures = "\n".join(
        f"- {f}" for f in (getattr(symptom_card, "observable_failures", []) or [])
    ) or "(none recorded)"
    parts = [
        f"## Target language\n{language}\n",
        f"## Problem statement (contains reproduction steps)\n{problem_statement}\n",
        f"## Observable failures (symptom card)\n{failures}\n",
        f"## Suspect entities (cited)\n" + ("\n".join(f"- {e}" for e in suspect_entities) or "(none)") + "\n",
    ]
    if template_text and template_path:
        parts.append(
            f"## Existing test template ({template_path})\n"
            "Copy this function's structure; change ONLY the input parameters to\n"
            "trigger the bug. Do NOT keep or add assertions about the result.\n"
            f"```\n{template_text[:6000]}\n```\n"
            "Produce a single runnable test (existing_test_template backend)."
        )
    else:
        parts.append(
            "## No template available\n"
            "Synthesize a standalone, independently-runnable trigger script\n"
            "(synthetic_script backend) that drives the suspect code with the\n"
            "reproduction input and lets the real error surface."
        )
    return "\n".join(parts)


async def synthesize_reproduction_script(
    symptom_card,
    problem_statement: str,
    language: BuildSystem,
    repo_dir: Path,
    suspect_entities: list[str],
    evidence: EvidenceCards,
) -> ReproductionSource | None:
    """Restricted-LLM translation of reproduction steps → a trigger script.

    Two backends, priority 1a > 1b (the doc's "照猫画虎 first"):
      1a existing_test_template (go/java first): copy a same-dir test, retarget
         inputs, run a single case with the native runner.
      1b synthetic_script (python first / fallback): standalone trigger script.

    Returns a ReproductionSource with the temp script written into repo_dir, or
    None when the LLM declines / the translation is infeasible (→ unverifiable).
    The LLM only TRANSLATES; it never judges grounding or asserts correctness.
    """
    from pydantic import BaseModel, Field

    from src.agents._structured import run_structured_query

    class _SynthScript(BaseModel):
        feasible: bool = Field(description="False if no runnable trigger can be produced.")
        backend: Literal["existing_test_template", "synthetic_script"] = Field(
            description="Which synthesis backend was used.",
        )
        filename: str = Field(
            default="",
            description="Suggested temp script filename (basename only, correct extension).",
        )
        script: str = Field(default="", description="Full runnable script text.")
        run_target: str = Field(
            default="",
            description=(
                "Native-runner selector for template backend, e.g. a go test "
                "function name (TestXxx) or maven Cls#method. Empty for "
                "standalone scripts."
            ),
        )

    adapter = LANG_ADAPTERS.get(language)
    if adapter is None:
        return None

    template_text: str | None = None
    template_path: str | None = None
    tpl = find_template_test(evidence, repo_dir, language)
    if tpl is not None:
        try:
            template_text = tpl.read_text(encoding="utf-8", errors="replace")
            template_path = _norm(str(tpl.relative_to(repo_dir)))
        except (OSError, ValueError):
            template_text = None
            template_path = None

    user_prompt = _synth_user_prompt(
        problem_statement, symptom_card, language,
        suspect_entities, template_text, template_path,
    )

    try:
        result = await run_structured_query(
            system_prompt=_SYNTH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=_SynthScript,
            component="dynamic-grounding-synth",
            allowed_tools=["Grep", "Read", "Glob"],
            max_turns=12,
            max_budget_usd=1.5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None

    if not result.feasible or not result.script.strip():
        return None

    # Write the script to a temp location inside repo_dir. For the template
    # backend the file must live beside its package (go/java need same-dir
    # compilation), so we place it in the template's directory; otherwise a
    # dedicated temp filename at repo root.
    suffix = adapter.script_suffix
    if result.backend == "existing_test_template" and template_path:
        tpl_dir = str(Path(template_path).parent)
        base = result.filename or f"repro_dynamic_grounding{suffix}"
        base = Path(base).name
        if not base.endswith(suffix):
            base += suffix
        rel = _norm(str(Path(tpl_dir) / base)) if tpl_dir not in ("", ".") else base
    else:
        base = result.filename or f"repro_dynamic_grounding{suffix}"
        base = Path(base).name
        if not base.endswith(suffix):
            base += suffix
        rel = base

    target = repo_dir / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.script, encoding="utf-8")
    except OSError:
        return None

    return ReproductionSource(
        backend=result.backend,
        language=language,
        script_relpath=rel,
        run_target=result.run_target.strip(),
        created_paths=[rel],
    )


# ── Top-level orchestration (one pass per case) ──────────────────────────────

def _all_cited_regions_by_req(evidence: EvidenceCards) -> dict[str, list[str]]:
    """Per active requirement, the union of cited regions to match the path against."""
    out: dict[str, list[str]] = {}
    for req in evidence.requirements:
        if req.verdict == "UNCHECKED":
            continue
        loc = req.scoped_evidence.localization
        regions: list[str] = []
        seen: set[str] = set()
        for entry in list(req.evidence_locations) + list(loc.exact_code_regions):
            path, start, _ = _parse_evidence_location(entry)
            if path and start != 0 and entry not in seen:
                seen.add(entry)
                regions.append(entry)
        if regions:
            out[req.id] = regions
    return out


async def run_dynamic_grounding(
    evidence: EvidenceCards | None,
    repo_dir: Path | None,
    problem_statement: str = "",
    timeout: int = 300,
) -> list[DynamicGroundingResult]:
    """Single dynamic-grounding pass for the whole case.

    Reproduces the bug ONCE on the base_commit tree, applies the symptom gate,
    and (only if a symptom is observed) mechanically matches the observed path
    against each active requirement's cited regions. ALWAYS restores the working
    tree afterward.

    Returns per-requirement DynamicGroundingResults. A case-level
    ``<global>`` ``dynamic_unverifiable_fallback`` is returned when no symptom
    could be reproduced — the caller treats that as "no opinion".
    """
    if evidence is None or repo_dir is None:
        return []
    repo_dir = Path(repo_dir)
    adapter = adapter_for(repo_dir)
    if adapter is None:
        return [DynamicGroundingResult(
            requirement_id="<global>",
            grounded_by="dynamic_unverifiable_fallback",
            detail="unknown build system — dynamic grounding skipped",
        )]

    cited_by_req = _all_cited_regions_by_req(evidence)
    if not cited_by_req:
        return [DynamicGroundingResult(
            requirement_id="<global>",
            grounded_by="dynamic_unverifiable_fallback",
            detail="no cited code regions on active requirements",
        )]

    suspect_entities: list[str] = []
    for req in evidence.requirements:
        suspect_entities += req.scoped_evidence.localization.suspect_entities

    source: ReproductionSource | None = None
    try:
        source = await synthesize_reproduction_script(
            evidence.symptom,
            problem_statement,
            adapter.name,
            repo_dir,
            suspect_entities,
            evidence,
        )
        if source is None:
            return [DynamicGroundingResult(
                requirement_id="<global>",
                grounded_by="dynamic_unverifiable_fallback",
                detail="reproduction script could not be synthesized",
            )]

        run = run_reproduction(repo_dir, source, adapter, timeout=timeout)

        # ── Symptom gate: the ONLY precondition for a strong reached signal ──
        if not observed_symptom(run, evidence.symptom):
            reason = (
                "toolchain missing" if run.toolchain_missing else
                "timed out" if run.timed_out else
                "setup error" if run.setup_error else
                "ran without exhibiting any symptom (silent pass / coverage-only)"
            )
            return [DynamicGroundingResult(
                requirement_id="<global>",
                grounded_by="dynamic_unverifiable_fallback",
                observed=False,
                exception_text=run.exception_text,
                detail=f"reproduction {reason}; static/AST results stand",
            )]

        # Symptom reproduced → mechanically match path per requirement.
        results: list[DynamicGroundingResult] = []
        for rid, regions in cited_by_req.items():
            reached, missed = match_path_reached(
                run.trace, run.coverage, regions, repo_dir,
            )
            if reached:
                results.append(DynamicGroundingResult(
                    requirement_id=rid,
                    grounded_by="dynamic_reached",
                    observed=True,
                    reached_locations=reached,
                    missed_locations=missed,
                    exception_text=run.exception_text,
                    detail=(
                        f"symptom reproduced ({run.exception_text[:80]!r}); "
                        f"failure path traversed cited {reached}"
                    ),
                ))
            else:
                results.append(DynamicGroundingResult(
                    requirement_id=rid,
                    grounded_by="dynamic_not_reached",
                    observed=True,
                    reached_locations=[],
                    missed_locations=missed,
                    exception_text=run.exception_text,
                    detail=(
                        f"symptom reproduced ({run.exception_text[:80]!r}); "
                        f"failure path did NOT traverse cited {missed} "
                        "(soft signal — repro may be incomplete)"
                    ),
                ))
        return results
    finally:
        created = source.created_paths if source is not None else []
        restore_worktree(repo_dir, created)
