"""SDK-aligned hook handlers for Orchestrator."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional

from ..contracts.phase_summary import PhaseSummary, SummaryStatus, TodoProposal
from ..contracts import TodoItem, TodoPriority, WorkflowState
from .conflict_detector import PhaseConflictDetector
from .state_store import StateStore


class Hooks:
    """Lifecycle hooks used by Orchestrator governance layer."""

    def __init__(self, state_store: StateStore, workflow_state: WorkflowState) -> None:
        self.state_store = state_store
        self.workflow_state = workflow_state
        self.failure_count = 0
        self.metadata: Dict[str, Any] = {"workers": [], "prompts": []}

    def on_stop(self, workflow_state: Optional[WorkflowState] = None) -> None:
        """Persist workflow state when execution stops."""

        self.state_store.save(workflow_state or self.workflow_state)

    def on_post_tool_use(self, llm_output: Any) -> PhaseSummary:
        """Parse phase summary from LLM output."""

        summary = self._parse_summary(llm_output)
        if summary.confidence < 0.5:
            self.workflow_state.routing_metadata["low_confidence"] = True
        self._persist_todos(summary)
        self.workflow_state.routing_metadata["last_summary"] = summary.model_dump()
        return summary

    def _persist_todos(self, summary: PhaseSummary) -> None:
        """Persist todos_proposed into workflow state with dedup by todo_id."""

        existing_ids = {todo.todo_id for todo in self.workflow_state.todos}
        for proposal in summary.todos_proposed:
            if proposal.id in existing_ids:
                continue
            todo = TodoItem(
                todo_id=proposal.id,
                source_type="llm",
                source_phase=summary.phase,
                priority=self._to_priority(proposal),
                title=proposal.title,
                description=proposal.reason,
                action_type="verify" if proposal.blocking else "enhance",
                target_phase=summary.recommended_next_phase,
            )
            self.workflow_state.todos.append(todo)
            existing_ids.add(proposal.id)

    def _to_priority(self, proposal: TodoProposal) -> TodoPriority:
        p = proposal.priority.lower()
        if p == "p0":
            return TodoPriority.P0
        if p == "p1":
            return TodoPriority.P1
        if p == "p3":
            return TodoPriority.P3
        return TodoPriority.P2

    def on_pre_tool_use(self, next_phase: str) -> tuple[bool, str, list[str]]:
        """Run pre-execution conflict checks."""

        detector = PhaseConflictDetector(self.workflow_state)
        hard = detector.check_hard_conflicts(next_phase)
        warnings, allow_soft = detector.check_soft_conflicts(next_phase)

        if hard.result == hard.result.BLOCK:
            suggested = hard.suggested_phase or self.workflow_state.current_phase
            return False, suggested, warnings

        return allow_soft, next_phase, warnings

    def on_post_tool_use_failure(self, error: Exception) -> int:
        """Track consecutive tool failures."""

        self.failure_count += 1
        self.workflow_state.routing_metadata["last_error"] = {
            "message": str(error),
            "at": datetime.utcnow().isoformat(),
            "count": self.failure_count,
        }
        return self.failure_count

    def on_subagent_start(self, worker_name: str) -> None:
        """Record worker lifecycle metadata."""

        self.metadata["workers"].append(
            {"worker": worker_name, "started_at": datetime.utcnow().isoformat()}
        )

    def on_user_prompt_submit(self, prompt: str) -> str:
        """Inject workflow context into user prompt."""

        self.metadata["prompts"].append(
            {"prompt": prompt, "submitted_at": datetime.utcnow().isoformat()}
        )
        return (
            f"{prompt}\n\n"
            f"[workflow_context] current_phase={self.workflow_state.current_phase}; "
            f"replan_iteration={self.workflow_state.replan_iteration}"
        )

    def _parse_summary(self, llm_output: Any) -> PhaseSummary:
        if isinstance(llm_output, PhaseSummary):
            return llm_output
        if isinstance(llm_output, dict):
            return PhaseSummary.model_validate(llm_output)

        text = str(llm_output)
        maybe_json = self._extract_first_json(text)
        if maybe_json is not None:
            try:
                return PhaseSummary.model_validate(maybe_json)
            except Exception:
                pass

        return PhaseSummary(
            phase=self.workflow_state.current_phase,
            status=SummaryStatus.PARTIAL,
            findings={"raw": text[:2000]},
            todos_proposed=[
                TodoProposal(
                    id=f"todo_parse_fallback_{int(datetime.utcnow().timestamp())}",
                    title="Review unstructured LLM output",
                    priority="p2",
                    reason="Model output did not match PhaseSummary schema",
                    blocking=False,
                )
            ],
            recommended_next_phase=self.workflow_state.current_phase,
            confidence=0.2,
            raw_text=text,
        )

    def _extract_first_json(self, text: str) -> Optional[Dict[str, Any]]:
        fenced = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                return None

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                return None
        return None
