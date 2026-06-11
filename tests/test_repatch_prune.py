"""Tests for repatch-round prompt-bloat mitigations (issue 010).

Two independent fixes, both no-mock / direct-call:
  1. `_prune_plan_to_error_files` — on a repatch round keep only the prior
     plan's edits implicated by the build errors, so the full plan is not
     re-inlined into the planner prompt (the bloat that crashed issue 010).
  2. `run_structured_query(allow_none=True)` — returns None instead of raising
     on the empty-structured_output path, letting the planner degrade to
     BUILD_FAILED rather than crashing the run.
"""

from __future__ import annotations

from src.orchestrator.build_verify import BuildError
from src.orchestrator.engine import _prune_plan_to_error_files
from src.models.patch import PatchPlan, FileEditPlan


def _plan(*paths: str) -> PatchPlan:
    return PatchPlan(
        overview="x",
        edits=[
            FileEditPlan(filepath=p, change_rationale="r")
            for p in paths
        ],
    )


def _err(file: str) -> BuildError:
    return BuildError(file=file, line=1, message="boom", raw="boom")


def test_prune_keeps_only_error_files():
    plan = _plan("a/x.go", "a/y.go", "a/z.go")
    errors = [_err("a/y.go")]
    pruned, dropped = _prune_plan_to_error_files(plan, errors)
    assert dropped == 2
    assert pruned is not None
    assert [e.filepath for e in pruned.edits] == ["a/y.go"]
    # Overview is preserved so the planner keeps the strategic context.
    assert pruned.overview == "x"


def test_prune_normalizes_paths():
    # Plan uses "./a/x.go", error uses "a/x.go" — they must match.
    plan = _plan("./a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("a/x.go")])
    assert dropped == 1
    assert [e.filepath for e in pruned.edits] == ["./a/x.go"]


def test_prune_returns_none_when_no_edit_matches():
    # All errors are in test files the plan never touched → nothing to keep.
    plan = _plan("a/x.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("a/x_test.go")])
    assert pruned is None
    assert dropped == 1


def test_prune_keeps_full_plan_when_all_files_error():
    plan = _plan("a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(
        plan, [_err("a/x.go"), _err("a/y.go")]
    )
    assert dropped == 0
    # Same object returned (no needless copy) when nothing is dropped.
    assert pruned is plan


def test_prune_synthetic_build_error_keeps_plan():
    # An un-attributable failure surfaces as file="(build)"; we cannot tell
    # which edits are implicated, so the plan is kept intact rather than
    # wrongly emptied.
    plan = _plan("a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("(build)")])
    assert pruned is plan
    assert dropped == 0


def test_prune_handles_empty_plan():
    assert _prune_plan_to_error_files(None, [_err("a/x.go")]) == (None, 0)
    empty = PatchPlan(overview="x", edits=[])
    assert _prune_plan_to_error_files(empty, [_err("a/x.go")]) == (empty, 0)
