"""Worker protocol contracts for orchestration runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkerActionType(str, Enum):
    """Action emitted by orchestrator for todo updates."""

    ADD = "add"
    RESOLVE = "resolve"
    BLOCK = "block"


class TodoAction(BaseModel):
    """Structured todo mutation action."""

    action: WorkerActionType
    todo_id: Optional[str] = None
    title: Optional[str] = None
    description: str = ""
    priority: str = "p2"
    source_phase: str = ""


class OrchestrationDecision(BaseModel):
    """Decision returned by LLM orchestrator each tick."""

    next_phase: str
    selected_workers: List[str] = Field(default_factory=list)
    todo_actions: List[TodoAction] = Field(default_factory=list)
    state_transition_reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class WorkerExecutionResult(BaseModel):
    """Standardized worker execution result."""

    worker_id: str
    success: bool
    outputs: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
