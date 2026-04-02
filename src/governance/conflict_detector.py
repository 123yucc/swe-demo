"""Phase conflict detection for Orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..contracts.phase_summary import ConflictResult, DeadloopResult
from ..contracts import PhaseStatus, WorkflowState


@dataclass
class DeadloopCheck:
    """Deadloop check outcome details."""

    result: DeadloopResult
    reason: str = ""


@dataclass
class ConflictCheckResult:
    """Conflict check outcome for routing decisions."""

    result: ConflictResult
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    suggested_phase: Optional[str] = None


class PhaseConflictDetector:
    """Guardrail checks for LLM proposed phase transitions."""

    def __init__(self, workflow_state: WorkflowState) -> None:
        self.workflow_state = workflow_state

    def check_hard_conflicts(self, proposed_phase: str) -> ConflictCheckResult:
        """Block transitions that violate hard constraints."""

        current_phase = self.workflow_state.current_phase
        unresolved_gaps = self.workflow_state.routing_metadata.get("unresolved_gaps", 0)
        patch_output_count = self.workflow_state.routing_metadata.get("patch_output_count", 0)
        validation_last_changed = self.workflow_state.routing_metadata.get("validation_last_changed", True)

        if current_phase.startswith("phase2") and proposed_phase.startswith("phase3"):
            sufficiency = self.workflow_state.routing_metadata.get("phase2_sufficiency", "partial")
            if sufficiency != "full":
                return ConflictCheckResult(
                    result=ConflictResult.BLOCK,
                    reason="Phase2 evidence not sufficient for Phase3",
                    suggested_phase="phase2",
                )

        if current_phase.startswith("phase3") and proposed_phase.startswith("phase4") and unresolved_gaps > 0:
            return ConflictCheckResult(
                result=ConflictResult.BLOCK,
                reason="Phase3 has unresolved gaps, cannot enter Phase4",
                suggested_phase="phase3",
            )

        if proposed_phase.startswith("phase6") and patch_output_count <= 0:
            return ConflictCheckResult(
                result=ConflictResult.BLOCK,
                reason="No patch output available for validation",
                suggested_phase="phase5",
            )

        if current_phase.startswith("phase6") and self.workflow_state.phase_status == PhaseStatus.FAILED and not validation_last_changed:
            return ConflictCheckResult(
                result=ConflictResult.BLOCK,
                reason="Validation failed without source changes; repetition blocked",
                suggested_phase="phase4",
            )

        return ConflictCheckResult(result=ConflictResult.ALLOW)

    def check_soft_conflicts(self, proposed_phase: str) -> tuple[List[str], bool]:
        """Return warnings for suspicious but still allowed transitions."""

        warnings: List[str] = []
        if self.workflow_state.replan_iteration >= 3 and proposed_phase.startswith("phase3"):
            warnings.append("Too many replans before entering Phase3")
        if self.workflow_state.routing_metadata.get("low_confidence", False):
            warnings.append("LLM confidence is low; monitor transition carefully")
        return warnings, True

    def detect_deadloop(self, phase_history: List[dict]) -> DeadloopCheck:
        """Detect short loops from recent phase history."""

        recent = [entry.get("phase") for entry in phase_history[-6:] if entry.get("phase")]
        if len(recent) < 6:
            return DeadloopCheck(result=DeadloopResult.SAFE)

        pattern = recent[-3:]
        if recent[-6:-3] == pattern:
            return DeadloopCheck(
                result=DeadloopResult.TRIGGERED,
                reason=f"Repeated phase pattern detected: {pattern}",
            )

        return DeadloopCheck(result=DeadloopResult.SAFE)
