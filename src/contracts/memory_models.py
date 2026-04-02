"""Memory contracts for working/long-term memory abstractions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkingMemorySnapshot(BaseModel):
    """Snapshot passed into orchestrator decisions."""

    instance_id: str
    current_phase: str
    unresolved_gaps: int = 0
    recent_decisions: List[str] = Field(default_factory=list)
    custom: Dict[str, Any] = Field(default_factory=dict)


class RetrievalRecord(BaseModel):
    """Record returned from long-term retrieval."""

    signal: str
    action: str
    confidence: float = 0.5
    evidence_refs: List[str] = Field(default_factory=list)


class LongTermMemoryQuery(BaseModel):
    """Query for long-term memory retrieval."""

    signal: str
    tags: List[str] = Field(default_factory=list)
    limit: int = 5
    module_scope: Optional[str] = None
