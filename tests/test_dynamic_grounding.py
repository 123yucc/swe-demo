"""Dynamic grounding tests (phase 26).

Real file-system fixtures + real subprocess reproduction — no mocks. The
restricted-LLM synthesis step (synthesize_reproduction_script) is the only part
that needs the network; every other piece is exercised directly by constructing
a ReproductionSource by hand and running it, mirroring the methodological
boundary: the grounding judgement is script-execution + mechanical comparison.

Language-specific runs (go / java / js) skip themselves when the toolchain is
absent — which doubly verifies the three-state contract (missing toolchain must
degrade to unverifiable, never a false fail).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    ScopedEvidence,
    StructuralCard,
    SymptomCard,
)
from src.orchestrator.dynamic_grounding import (
    LANG_ADAPTERS,
    ReproductionSource,
    RunResult,
    adapter_for,
    match_path_reached,
    observed_symptom,
    parse_go_trace,
    parse_java_trace,
    parse_js_trace,
    parse_python_trace,
    restore_worktree,
    run_dynamic_grounding,
    run_reproduction,
)


def _write(repo: Path, rel: str, content: str) -> None:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


def _cards(*reqs: RequirementItem, symptom: SymptomCard | None = None) -> EvidenceCards:
    return EvidenceCards(
        symptom=symptom or SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(reqs),
    )


# ── Trace parsers (primary signal) ──────────────────────────────────────────

def test_parse_python_trace():
    tb = (
        'Traceback (most recent call last):\n'
        '  File "app/calc.py", line 7, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    frames = parse_python_trace(tb)
    assert ("app/calc.py", 7) in frames


def test_parse_go_trace():
    out = (
        "panic: runtime error: index out of range [3]\n\n"
        "goroutine 1 [running]:\n"
        "main.boom(...)\n"
        "\t/app/pkg/buggy.go:12 +0x1d\n"
    )
    frames = parse_go_trace(out)
    assert ("/app/pkg/buggy.go", 12) in frames


def test_parse_java_trace():
    out = (
        "Exception in thread \"main\" java.lang.NullPointerException\n"
        "\tat com.example.Calc.divide(Calc.java:15)\n"
        "\tat com.example.Main.main(Main.java:8)\n"
    )
    frames = parse_java_trace(out)
    assert ("Calc.java", 15) in frames
    assert ("Main.java", 8) in frames


def test_parse_js_trace():
    out = (
        "TypeError: Cannot read properties of undefined\n"
        "    at divide (/app/src/calc.js:4:18)\n"
        "    at Object.<anonymous> (/app/src/index.js:2:1)\n"
    )
    frames = parse_js_trace(out)
    assert ("/app/src/calc.js", 4) in frames


# ── observed_symptom: the symptom gate ──────────────────────────────────────

def test_observed_symptom_true_on_exception():
    run = RunResult(
        returncode=1,
        output="ZeroDivisionError: division by zero",
        trace=[("calc.py", 7)],
        exception_text="ZeroDivisionError: division by zero",
    )
    assert observed_symptom(run, SymptomCard(
        observable_failures=["division by zero raises ZeroDivisionError"]
    )) is True


def test_observed_symptom_false_on_clean_pass():
    # rc==0, no exception → reachability only, NOT a symptom.
    run = RunResult(returncode=0, output="ok", trace=[], exception_text="")
    assert observed_symptom(run, SymptomCard(observable_failures=["boom"])) is False


def test_observed_symptom_false_on_toolchain_missing():
    run = RunResult(returncode=127, output="not found", toolchain_missing=True)
    assert observed_symptom(run, SymptomCard()) is False


def test_observed_symptom_false_on_timeout():
    run = RunResult(returncode=124, output="", timed_out=True)
    assert observed_symptom(run, SymptomCard()) is False


# ── match_path_reached: mechanical comparison ───────────────────────────────

_BUGGY_PY = '''\
def helper(x):
    return x + 1


def divide(a, b):
    result = a / b
    return result


def unrelated(z):
    return z * 2
'''


def test_match_reached_on_exact_line(tmp_path: Path):
    _write(tmp_path, "calc.py", _BUGGY_PY)
    # divide spans lines 5-7; the frame is line 6 (a / b).
    trace = [("calc.py", 6)]
    reached, missed = match_path_reached(trace, {}, ["calc.py:6"], tmp_path)
    assert reached == ["calc.py:6"]
    assert missed == []


def test_match_reached_via_def_span(tmp_path: Path):
    _write(tmp_path, "calc.py", _BUGGY_PY)
    # Cited region is the divide signature line (5); frame is line 6, inside the
    # same function's def-span → function-granularity match.
    trace = [("calc.py", 6)]
    reached, missed = match_path_reached(trace, {}, ["calc.py:5"], tmp_path)
    assert reached == ["calc.py:5"]


def test_match_not_reached_on_unrelated_region(tmp_path: Path):
    _write(tmp_path, "calc.py", _BUGGY_PY)
    # Frame in divide (line 6); cited region is unrelated() at lines 10-11.
    trace = [("calc.py", 6)]
    reached, missed = match_path_reached(trace, {}, ["calc.py:10-11"], tmp_path)
    assert reached == []
    assert missed == ["calc.py:10-11"]


def test_match_reached_via_coverage_only(tmp_path: Path):
    _write(tmp_path, "calc.py", _BUGGY_PY)
    # No stack frame, but coverage recorded the line — still a hit.
    reached, missed = match_path_reached([], {"calc.py": {6}}, ["calc.py:6"], tmp_path)
    assert reached == ["calc.py:6"]


# ── run_reproduction: real Python subprocess ────────────────────────────────

def _python_source(repo: Path, script_rel: str) -> ReproductionSource:
    return ReproductionSource(
        backend="synthetic_script",
        language="python",
        script_relpath=script_rel,
        created_paths=[script_rel],
    )


def test_run_reproduction_python_captures_symptom(tmp_path: Path):
    _write(tmp_path, "calc.py", _BUGGY_PY)
    _write(
        tmp_path,
        "repro.py",
        "from calc import divide\n"
        "print(divide(1, 0))\n",
    )
    adapter = LANG_ADAPTERS["python"]
    run = run_reproduction(tmp_path, _python_source(tmp_path, "repro.py"), adapter)
    assert run.returncode != 0
    assert "ZeroDivisionError" in run.exception_text
    # The traceback frame lands in calc.py inside divide.
    assert any(f == "repro.py" or f.endswith("calc.py") for f, _ in run.trace)


def test_run_reproduction_python_silent_pass_is_not_symptom(tmp_path: Path):
    """Symptom-gate key case: a clean run with coverage on the cited line must
    NOT be treated as reached — reachability does not impersonate a symptom."""
    _write(tmp_path, "calc.py", _BUGGY_PY)
    _write(
        tmp_path,
        "repro_ok.py",
        "from calc import divide\n"
        "print(divide(6, 2))\n",  # no error — passes silently
    )
    adapter = LANG_ADAPTERS["python"]
    run = run_reproduction(tmp_path, _python_source(tmp_path, "repro_ok.py"), adapter)
    assert run.returncode == 0
    assert observed_symptom(run, SymptomCard(
        observable_failures=["divide raises ZeroDivisionError"]
    )) is False


def test_run_reproduction_missing_toolchain_is_unverifiable(tmp_path: Path):
    """A drive command whose binary is absent → rc=127 → not a symptom."""
    src = ReproductionSource(
        backend="synthetic_script",
        language="python",
        script_relpath="x.py",
    )
    fake_adapter = LANG_ADAPTERS["python"]
    # Build an adapter variant whose drive command points at a missing binary.
    import dataclasses

    bad = dataclasses.replace(
        fake_adapter,
        drive_cmd=lambda _s: ["definitely-not-a-real-binary-xyz", "x.py"],
    )
    run = run_reproduction(tmp_path, src, bad)
    assert run.toolchain_missing is True
    assert observed_symptom(run, SymptomCard()) is False


# ── adapter selection / opt-in ──────────────────────────────────────────────

def test_adapter_for_python_repo(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    adapter = adapter_for(tmp_path)
    assert adapter is not None and adapter.name == "python"


def test_adapter_for_unknown_repo_is_none(tmp_path: Path):
    _write(tmp_path, "README.md", "nothing here")
    assert adapter_for(tmp_path) is None


# ── run_dynamic_grounding LLM-free paths ────────────────────────────────────

def test_run_dynamic_grounding_unknown_build_system_unverifiable(tmp_path: Path):
    _write(tmp_path, "README.md", "no build system")
    req = RequirementItem(
        id="req-001", text="x", origin="requirements",
        verdict="AS_IS_VIOLATED", evidence_locations=["a.py:1"],
    )
    results = asyncio.run(run_dynamic_grounding(_cards(req), tmp_path))
    assert len(results) == 1
    assert results[0].requirement_id == "<global>"
    assert results[0].grounded_by == "dynamic_unverifiable_fallback"


def test_run_dynamic_grounding_no_cited_regions_unverifiable(tmp_path: Path):
    # Python repo, but no requirement carries a parseable cited region → the
    # function returns before any LLM synthesis.
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    req = RequirementItem(
        id="req-001", text="x", origin="requirements", verdict="AS_IS_COMPLIANT",
    )
    results = asyncio.run(run_dynamic_grounding(_cards(req), tmp_path))
    assert len(results) == 1
    assert results[0].grounded_by == "dynamic_unverifiable_fallback"


def test_run_dynamic_grounding_none_inputs():
    assert asyncio.run(run_dynamic_grounding(None, None)) == []


# ── restore_worktree: base_commit hygiene ───────────────────────────────────

@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_restore_worktree_cleans_temp_and_restores(tmp_path: Path):
    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, check=False,
        )

    git("init")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    _write(tmp_path, "tracked.py", "x = 1\n")
    git("add", "-A")
    git("commit", "-m", "base")

    # Simulate a reproduction run that wrote a temp script + coverage products
    # and dirtied a tracked file.
    _write(tmp_path, "repro_dynamic_grounding.py", "print('boom')\n")
    _write(tmp_path, "coverage.out", "mode: set\n")
    (tmp_path / "tracked.py").write_text("x = 999\n", encoding="utf-8")

    restore_worktree(tmp_path, ["repro_dynamic_grounding.py"])

    assert not (tmp_path / "repro_dynamic_grounding.py").exists()
    assert not (tmp_path / "coverage.out").exists()
    assert (tmp_path / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    assert status.stdout.strip() == ""


# ── Go end-to-end (skipped without go toolchain) ────────────────────────────

@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not available")
def test_go_reproduction_panic_reached(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/buggy\n\ngo 1.20\n")
    _write(
        tmp_path,
        "pkg/buggy.go",
        "package pkg\n"
        "\n"
        "func Boom(s []int) int {\n"
        "\treturn s[10]\n"  # index out of range on a short slice
        "}\n",
    )
    # Standalone trigger program (synthetic_script backend → `go run`).
    _write(
        tmp_path,
        "repro_main.go",
        "package main\n"
        "\n"
        "import \"example.com/buggy/pkg\"\n"
        "\n"
        "func main() {\n"
        "\tpkg.Boom([]int{1, 2, 3})\n"
        "}\n",
    )
    src = ReproductionSource(
        backend="synthetic_script", language="go",
        script_relpath="repro_main.go", created_paths=["repro_main.go"],
    )
    adapter = LANG_ADAPTERS["go"]
    run = run_reproduction(tmp_path, src, adapter)
    assert run.returncode != 0
    assert "panic" in run.exception_text.lower()
    assert observed_symptom(run, SymptomCard(
        observable_failures=["index out of range panic"]
    )) is True
    # The panic frame should land in buggy.go line 4 (return s[10]).
    reached, missed = match_path_reached(
        run.trace, run.coverage, ["pkg/buggy.go:4"], tmp_path,
    )
    assert reached == ["pkg/buggy.go:4"]


# ── JS end-to-end (skipped without node) ────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_js_reproduction_throw_reached(tmp_path: Path):
    _write(tmp_path, "package.json", '{"name":"buggy","version":"1.0.0"}\n')
    _write(
        tmp_path,
        "calc.js",
        "function divide(a, b) {\n"
        "  if (b === 0) { throw new TypeError('division by zero'); }\n"
        "  return a / b;\n"
        "}\n"
        "module.exports = { divide };\n",
    )
    _write(
        tmp_path,
        "repro.js",
        "const { divide } = require('./calc');\n"
        "divide(1, 0);\n",
    )
    src = ReproductionSource(
        backend="synthetic_script", language="node",
        script_relpath="repro.js", created_paths=["repro.js"],
    )
    adapter = LANG_ADAPTERS["node"]
    run = run_reproduction(tmp_path, src, adapter)
    assert run.returncode != 0
    assert "TypeError" in run.exception_text
    assert observed_symptom(run, SymptomCard(
        observable_failures=["division by zero throws TypeError"]
    )) is True
    reached, missed = match_path_reached(
        run.trace, run.coverage, ["calc.js:2"], tmp_path,
    )
    assert reached == ["calc.js:2"]
