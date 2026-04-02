"""Deprecated compatibility module.

Use src/contracts/workflow.py for runtime contracts and src/workers/registry.py for WorkerSpec registry.
"""

from .workflow import (
    PhaseStatus,
    TaskSpec,
    TaskStatus,
    TodoItem,
    TodoPriority,
    TodoStatus,
    WorkerExecutionMode,
    WorkerSpec,
    WorkflowState,
)

__all__ = [
    "WorkflowState",
    "WorkerSpec",
    "WorkerExecutionMode",
    "TaskSpec",
    "TodoItem",
    "TodoStatus",
    "TodoPriority",
    "PhaseStatus",
    "TaskStatus",
]
