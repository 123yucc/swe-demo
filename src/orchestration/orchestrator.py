"""LLM-first orchestrator for closed-loop workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set

from ..adapters.llm_client import propose_orchestration_decision
from ..contracts.worker_protocol import OrchestrationDecision
from ..contracts.workflow import PhaseStatus, TaskStatus, WorkflowState
from ..governance.state_store import StateStore
from ..memory.manager import MemoryManager
from ..workers.registry import create_default_worker_specs
from .event_bus import EventBus
from .phase_router import PhaseRouter
from .state_machine import LoopState
from .todo_manager import TodoManager
from .worker_runtime import WorkerRuntime


@dataclass
class LLMOrchestrationResult:
    success: bool
    final_state: str
    completed_workers: List[str] = field(default_factory=list)
    failed_workers: List[str] = field(default_factory=list)
    iterations: int = 0


class LLMOrchestrator:
    """Orchestrator as a single LLM decision-maker."""

    def __init__(self, workspace_dir: str, instance_id: str) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.instance_id = instance_id
        if self.workspace_dir.name == instance_id:
            self.instance_dir = self.workspace_dir
        else:
            self.instance_dir = self.workspace_dir / instance_id

        self.worker_specs = create_default_worker_specs()
        self.runtime = WorkerRuntime()
        self.todo_manager = TodoManager()
        self.router = PhaseRouter()
        self.memory = MemoryManager(str(self.workspace_dir), instance_id)
        self.state_store = StateStore(self.workspace_dir)
        self.event_bus = EventBus(self.instance_dir / "logs" / "llm_orchestrator_events.jsonl")

        self.state = WorkflowState(instance_id=instance_id)
        self.current_loop_state = LoopState.INIT
        self.completed: Set[str] = set()
        self.failed: Set[str] = set()

    def _ready_workers(self) -> List[str]:
        ready: List[str] = []
        for worker_id, spec in self.worker_specs.items():
            if worker_id in self.completed or worker_id in self.failed:
                continue
            if all(dep in self.completed for dep in spec.depends_on):
                ready.append(worker_id)
        return ready

    async def run(self, max_iterations: int = 20) -> LLMOrchestrationResult:
        self.memory.on_workflow_start(self.instance_id)

        for iteration in range(1, max_iterations + 1):
            ready = self._ready_workers()
            if not ready:
                if "validator" in self.completed:
                    self.current_loop_state = LoopState.PATCH_SUCCESS
                break

            context: Dict[str, Any] = {
                "instance_id": self.instance_id,
                "current_phase": self.state.current_phase,
                "loop_state": self.current_loop_state.value,
                "ready_workers": ready,
                "completed_workers": sorted(self.completed),
                "routing_metadata": self.state.routing_metadata,
                "unresolved_gaps": len(self.memory.get_unresolved_gaps()),
                "retrieval": [
                    {
                        "signal": item.signal,
                        "action": item.action,
                        "confidence": item.confidence,
                    }
                    for item in self.memory.get_patterns_for_signal(self.state.current_phase)[:5]
                ],
            }

            decision: OrchestrationDecision = await propose_orchestration_decision(context)
            requested_state = LoopState(decision.next_phase) if decision.next_phase in {s.value for s in LoopState} else self.current_loop_state
            check = self.router.check(
                current=self.current_loop_state,
                requested=requested_state,
                has_patch_output=("patch-executor" in self.completed),
                closure_passed=("closure-checker" in self.completed),
            )

            if check.allowed:
                self.current_loop_state = check.target_state
            else:
                self.state.routing_metadata["blocked_reason"] = check.reason

            for action in decision.todo_actions:
                self.todo_manager.apply(self.state, action)

            selected = [worker_id for worker_id in decision.selected_workers if worker_id in ready] or ready[:1]

            for worker_id in selected:
                result = await self.runtime.run(self.worker_specs[worker_id], str(self.instance_dir), self.instance_id)
                if result.success:
                    self.completed.add(worker_id)
                    self.state.worker_status[worker_id] = TaskStatus.COMPLETED
                    self.state.worker_results[worker_id] = result.outputs
                else:
                    self.failed.add(worker_id)
                    self.state.worker_status[worker_id] = TaskStatus.FAILED
                    self.state.failure_reason = result.error

            self.state.current_phase = decision.next_phase
            self.state.phase_status = PhaseStatus.IN_PROGRESS if not self.failed else PhaseStatus.FAILED
            self.state.replan_iteration = iteration - 1
            self.state.routing_metadata["last_reason"] = decision.state_transition_reason

            self.event_bus.emit(
                "orchestration_tick",
                {
                    "iteration": iteration,
                    "loop_state": self.current_loop_state.value,
                    "selected_workers": selected,
                    "completed": sorted(self.completed),
                    "failed": sorted(self.failed),
                },
            )
            self.state_store.save(self.state)

            if self.current_loop_state in {LoopState.PATCH_SUCCESS, LoopState.END}:
                break

        success = len(self.failed) == 0
        self.state.phase_status = PhaseStatus.COMPLETED if success else PhaseStatus.FAILED
        self.state_store.save(self.state)

        return LLMOrchestrationResult(
            success=success,
            final_state=self.current_loop_state.value,
            completed_workers=sorted(self.completed),
            failed_workers=sorted(self.failed),
            iterations=self.state.replan_iteration + 1,
        )
