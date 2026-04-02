"""Todo action applier for orchestration decisions."""

from __future__ import annotations

from datetime import datetime

from ..contracts.worker_protocol import TodoAction, WorkerActionType
from ..contracts.workflow import TodoItem, TodoPriority, TodoStatus, WorkflowState


def _to_priority(priority: str) -> TodoPriority:
    p = priority.lower()
    if p == "p0":
        return TodoPriority.P0
    if p == "p1":
        return TodoPriority.P1
    if p == "p3":
        return TodoPriority.P3
    return TodoPriority.P2


class TodoManager:
    """Apply todo actions to workflow state."""

    def apply(self, state: WorkflowState, action: TodoAction) -> None:
        if action.action == WorkerActionType.ADD:
            todo = TodoItem(
                todo_id=action.todo_id or f"todo_{int(datetime.utcnow().timestamp())}",
                source_type="llm",
                source_phase=action.source_phase,
                priority=_to_priority(action.priority),
                title=action.title or "LLM proposed action",
                description=action.description,
                action_type="enhance",
            )
            state.todos.append(todo)
            return

        if not action.todo_id:
            return

        todo = state.get_todo_by_id(action.todo_id)
        if not todo:
            return

        if action.action == WorkerActionType.RESOLVE:
            todo.status = TodoStatus.DONE
            todo.completed_at = datetime.utcnow().isoformat()
        elif action.action == WorkerActionType.BLOCK:
            todo.status = TodoStatus.BLOCKED
