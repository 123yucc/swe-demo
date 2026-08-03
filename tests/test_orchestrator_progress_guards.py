from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    StructuralCard,
    SymptomCard,
)
from src.models.memory import ActionEvent
from src.models.verdict import ClosureVerdict
from src.orchestrator import engine
from src.orchestrator.guards import DeepSearchBudget
from src.orchestrator.states import PipelineState
from src.tools.ingestion_tools import init_working_memory, reset_submitted_evidence


def _cards(*requirements: RequirementItem) -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(requirements),
    )


def test_checkpoint_counter_uses_live_iteration():
    budget = DeepSearchBudget(max_iterations=30)
    budget.record_iteration()
    budget.record_iteration()

    counters = engine._pack_counters(budget, 0, 0, 0, {}, 0)

    assert counters["deep_search_iterations_done"] == 2


def test_legacy_zero_checkpoint_recovers_iteration_from_action_history():
    history = [
        ActionEvent(
            phase="deep-search",
            subagent="deep-search",
            outcome="iter17:TO_BE_MISSING",
            requirement_id="req-001",
        ),
        ActionEvent(
            phase="closure-check",
            subagent="closure-checker",
            outcome="EVIDENCE_MISSING",
        ),
    ]

    assert engine._restore_deep_search_iteration(0, history) == 17
    assert engine._restore_deep_search_iteration(23, history) == 23


def test_deep_search_budget_covers_large_requirement_sets_with_rework_headroom():
    assert engine._deep_search_iteration_limit(0) == 30
    assert engine._deep_search_iteration_limit(20) == 30
    assert engine._deep_search_iteration_limit(31) == 41
    assert engine._deep_search_iteration_limit(50) == 60


def test_compile_repair_bonus_requires_small_decreasing_error_set():
    assert engine._should_run_compile_repair(2, 3, 20, 30)
    assert engine._should_run_compile_repair(3, 3, 3, 11)
    assert not engine._should_run_compile_repair(3, 3, 4, 11)
    assert not engine._should_run_compile_repair(3, 3, 3, 3)
    assert not engine._should_run_compile_repair(4, 3, 1, 3)


def test_force_exhausted_preserves_count_and_sets_forced_marker():
    budget = DeepSearchBudget(max_iterations=30)

    budget.force_exhausted()

    assert budget.iteration == 0
    assert budget.is_exhausted()
    assert budget.budget_exhausted


def test_frozen_unchecked_requirement_forces_closure_without_spinning(
    tmp_path, monkeypatch,
):
    reset_submitted_evidence()
    evidence = _cards(
        RequirementItem(
            id="req-004",
            text="Investigate the frozen requirement.",
            origin="requirements",
            verdict="UNCHECKED",
        )
    )
    memory = init_working_memory("issue", evidence)
    memory.action_history.append(
        ActionEvent(
            phase="deep-search",
            subagent="deep-search",
            outcome="frozen_stalled:3_visits",
            requirement_id="req-004",
        )
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")

    closure = AsyncMock(
        return_value=ClosureVerdict(
            verdict="CLOSURE_APPROVED",
            rationale="Test-only forced closure.",
        )
    )
    monkeypatch.setattr(engine, "_run_closure_checker_async", closure)

    result = asyncio.run(
        engine._run_state_machine(
            issue_id="issue",
            repo_dir=tmp_path,
            output_dir=tmp_path,
            evidence_path=evidence_path,
            memory=memory,
            initial_state=PipelineState.UNDER_SPECIFIED,
            stop_after_closure=True,
        )
    )

    assert result == evidence_path
    assert closure.await_count == 1
    assert any(
        event.outcome == "forced_closure:no_selectable_unchecked_target"
        for event in memory.action_history
    )
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["budget_counters"]["deep_search_iterations_done"] == 0
