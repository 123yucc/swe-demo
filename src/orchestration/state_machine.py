"""Explicit state machine for repair closure loop."""

from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class LoopState(str, Enum):
    INIT = "init"
    UNDER_SPECIFIED = "under_specified"
    EVIDENCE_REFINING = "evidence_refining"
    CLOSED = "closed"
    PATCH_FAILED = "patch_failed"
    PATCH_SUCCESS = "patch_success"
    END = "end"


_ALLOWED: Dict[LoopState, Set[LoopState]] = {
    LoopState.INIT: {LoopState.UNDER_SPECIFIED, LoopState.EVIDENCE_REFINING},
    LoopState.UNDER_SPECIFIED: {LoopState.EVIDENCE_REFINING},
    LoopState.EVIDENCE_REFINING: {LoopState.UNDER_SPECIFIED, LoopState.CLOSED},
    LoopState.CLOSED: {LoopState.PATCH_FAILED, LoopState.PATCH_SUCCESS},
    LoopState.PATCH_FAILED: {LoopState.EVIDENCE_REFINING, LoopState.CLOSED},
    LoopState.PATCH_SUCCESS: {LoopState.END},
    LoopState.END: set(),
}


def can_transition(current: LoopState, target: LoopState) -> bool:
    """Check whether transition is allowed by topology."""
    return target in _ALLOWED[current]
