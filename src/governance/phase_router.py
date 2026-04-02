"""Fallback phase routing rules for Orchestrator."""

from __future__ import annotations

from ..contracts import WorkflowState


class PhaseRouter:
    """Apply deterministic fallback routing when LLM suggestion is unsafe."""

    def __init__(self, workflow_state: WorkflowState) -> None:
        self.workflow_state = workflow_state

    def apply_fallback_rules(self, llm_recommendation: str | None) -> str:
        """Return actual next phase after fallback rules."""

        if not llm_recommendation:
            return self._default_next_phase()

        if self.workflow_state.routing_metadata.get("force_phase"):
            return str(self.workflow_state.routing_metadata["force_phase"])

        unresolved_gaps = int(self.workflow_state.routing_metadata.get("unresolved_gaps", 0))
        if unresolved_gaps > 0 and llm_recommendation in {"phase4", "phase5", "phase6"}:
            return "phase3"

        if llm_recommendation == "phase6" and int(self.workflow_state.routing_metadata.get("patch_output_count", 0)) <= 0:
            return "phase5"

        if self.workflow_state.routing_metadata.get("validation_failed", False):
            retries = int(self.workflow_state.routing_metadata.get("validation_retry", 0))
            if retries >= 2:
                return "phase4"
            return "phase5"

        return llm_recommendation

    def _default_next_phase(self) -> str:
        phase_order = ["phase1", "phase2", "phase3", "phase4", "phase5", "phase6"]
        current = self.workflow_state.current_phase
        if current not in phase_order:
            return "phase1"
        idx = phase_order.index(current)
        return phase_order[min(idx + 1, len(phase_order) - 1)]
