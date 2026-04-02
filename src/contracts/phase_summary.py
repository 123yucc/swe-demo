"""Phase summary contracts for orchestration."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SummaryStatus(str, Enum):
    """Status returned by a phase worker."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PARTIAL = "partial"


class ConflictResult(str, Enum):
    """Decision returned by conflict checks."""

    ALLOW = "allow"
    BLOCK = "block"
    WARN = "warn"


class DeadloopResult(str, Enum):
    """Deadloop detection result."""

    TRIGGERED = "triggered"
    SAFE = "safe"


class TodoProposal(BaseModel):
    """LLM-proposed todo item in phase summary."""

    id: str = Field(..., description="Unique todo id")
    title: str = Field(..., description="Human-readable todo title")
    priority: str = Field(default="p2", description="Priority p0-p3")
    affected_phases: List[str] = Field(default_factory=list)
    reason: str = Field(default="", description="Why this todo matters")
    blocking: bool = Field(default=False, description="Whether this todo blocks progress")


class PhaseSummary(BaseModel):
    """Structured phase summary produced by LLM workers."""

    phase: str = Field(..., description="Phase id such as phase1")
    status: SummaryStatus = Field(default=SummaryStatus.COMPLETED)
    findings: Dict[str, Any] = Field(default_factory=dict)
    todos_proposed: List[TodoProposal] = Field(default_factory=list)
    blocked_by: List[str] = Field(default_factory=list)
    recommended_next_phase: Optional[str] = Field(default=None)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_text: Optional[str] = Field(default=None, description="Original model output")


class ConflictCheck(BaseModel):
    """Detailed conflict check result."""

    result: ConflictResult
    reason: str = ""
    warnings: List[str] = Field(default_factory=list)
    suggested_phase: Optional[str] = None
