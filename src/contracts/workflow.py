"""Workflow contracts as the single source of truth."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TodoStatus(str, Enum):
    """Todo status."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


class TodoPriority(str, Enum):
    """Todo priority."""

    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class PhaseStatus(str, Enum):
    """Phase status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(str, Enum):
    """Task status."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"


class WorkerExecutionMode(str, Enum):
    """Worker execution mode."""

    PYTHON = "python"
    CLAUDE_AGENT = "claude_agent"


class TodoItem(BaseModel):
    """Todo item in workflow."""

    todo_id: str = Field(..., description="Todo ID")
    source_type: str = Field(..., description="Source type")
    source_phase: str = Field(..., description="Source phase")
    priority: TodoPriority = Field(default=TodoPriority.P2)
    status: TodoStatus = Field(default=TodoStatus.PENDING)
    title: str = Field(...)
    description: str = Field(default="")
    action_type: str = Field(...)
    target_card: Optional[str] = Field(default=None)
    target_phase: Optional[str] = Field(default=None)
    depends_on: List[str] = Field(default_factory=list)
    blocks: List[str] = Field(default_factory=list)
    result: Optional[Dict[str, Any]] = Field(default=None)
    error_info: Optional[Dict[str, Any]] = Field(default=None)
    retry_count: int = Field(default=0)
    max_retries: int = Field(default=3)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = Field(default=None)
    completed_at: Optional[str] = Field(default=None)
    validation_path: Optional[str] = Field(default=None)

    def is_ready(self) -> bool:
        return self.status == TodoStatus.PENDING and self.retry_count < self.max_retries

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries


class WorkerSpec(BaseModel):
    """Unified worker specification used for runtime + SDK agent generation."""

    worker_id: str = Field(...)
    phase: str = Field(...)
    description: str = Field(default="")
    depends_on: List[str] = Field(default_factory=list)
    gate_conditions: List[str] = Field(default_factory=list)
    can_parallel: bool = Field(default=False)
    max_retries: int = Field(default=3)
    timeout: int = Field(default=300)
    produces_cards: List[str] = Field(default_factory=list)
    produces_todos: List[str] = Field(default_factory=list)
    executor: Optional[str] = Field(default=None)

    # Single-source fields for Claude SDK dynamic agent generation
    execution_mode: WorkerExecutionMode = Field(default=WorkerExecutionMode.CLAUDE_AGENT)
    model: Literal["sonnet", "opus", "haiku", "inherit"] = Field(default="sonnet")
    prompt_template: str = Field(default="")
    allowed_tools: List[str] = Field(default_factory=lambda: ["Read"])
    input_schema_ref: Optional[str] = Field(default=None)
    output_schema_ref: Optional[str] = Field(default=None)

    def __hash__(self) -> int:
        return hash(self.worker_id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WorkerSpec):
            return self.worker_id == other.worker_id
        return False


class TaskSpec(BaseModel):
    """Execution task record."""

    task_id: str = Field(...)
    worker_id: str = Field(...)
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = Field(default=None)
    completed_at: Optional[str] = Field(default=None)
    duration_ms: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)
    retry_count: int = Field(default=0)
    decisions: List[Dict[str, Any]] = Field(default_factory=list)


class WorkflowState(BaseModel):
    """Workflow runtime state."""

    instance_id: str = Field(...)
    schema_version: str = Field(default="1.0")
    current_phase: str = Field(default="init")
    phase_status: PhaseStatus = Field(default=PhaseStatus.PENDING)
    phase_history: List[Dict[str, Any]] = Field(default_factory=list)
    worker_status: Dict[str, TaskStatus] = Field(default_factory=dict)
    worker_results: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    todos: List[TodoItem] = Field(default_factory=list)
    resources: Dict[str, Any] = Field(default_factory=dict)
    routing_metadata: Dict[str, Any] = Field(default_factory=dict)
    replan_iteration: int = Field(default=0)
    failure_reason: Optional[str] = Field(default=None)
    failure_phase: Optional[str] = Field(default=None)
    card_versions: Dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def update_timestamp(self) -> None:
        self.updated_at = datetime.utcnow().isoformat()

    def add_phase_history(self, phase: str, status: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.phase_history.append(
            {
                "phase": phase,
                "status": status,
                "timestamp": datetime.utcnow().isoformat(),
                "details": details or {},
            }
        )
        self.update_timestamp()

    def get_pending_todos(self) -> List[TodoItem]:
        return [todo for todo in self.todos if todo.status in (TodoStatus.PENDING, TodoStatus.READY)]

    def get_todo_by_id(self, todo_id: str) -> Optional[TodoItem]:
        for todo in self.todos:
            if todo.todo_id == todo_id:
                return todo
        return None

    @classmethod
    def load(cls, path: Path) -> "WorkflowState":
        if not path.exists():
            raise FileNotFoundError(f"Workflow state not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        if "worker_status" in data:
            data["worker_status"] = {k: TaskStatus(v) for k, v in data["worker_status"].items()}
        if "todos" in data:
            data["todos"] = [TodoItem(**item) for item in data["todos"]]
        if "phase_status" in data:
            data["phase_status"] = PhaseStatus(data["phase_status"])
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
