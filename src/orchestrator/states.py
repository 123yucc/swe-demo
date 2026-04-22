"""
PipelineState: code-driven state machine for the repair harness.

Replaces the LLM-defined state machine from ORCHESTRATOR_SYSTEM_PROMPT.
Code enforces transition legality and allowed subagent types — the LLM
cannot bypass these constraints.
"""

from __future__ import annotations

from enum import Enum


class PipelineState(str, Enum):
    """Pipeline states — the only valid states the orchestrator can be in."""

    UNDER_SPECIFIED = "UnderSpecified"
    EVIDENCE_REFINING = "EvidenceRefining"
    CLOSED = "Closed"
    PATCH_PLANNING = "PatchPlanning"
    PATCH_SUCCESS = "PatchSuccess"
    PATCH_FAILED = "PatchFailed"
    CLOSURE_FORCED_FAIL = "ClosureForcedFail"


# ── Transition table ─────────────────────────────────────────────────────
# Keys are the current state; values are the set of states that may follow.
# Any transition not in this table is illegal and will be rejected by code.

ALLOWED_TRANSITIONS: dict[PipelineState, set[PipelineState]] = {
    PipelineState.UNDER_SPECIFIED: {
        PipelineState.EVIDENCE_REFINING,
    },
    PipelineState.EVIDENCE_REFINING: {
        PipelineState.UNDER_SPECIFIED,       # closure-checker says EVIDENCE_MISSING
        PipelineState.CLOSED,                # closure-checker says CLOSURE_APPROVED
        PipelineState.CLOSURE_FORCED_FAIL,   # budget exhausted + still missing
    },
    PipelineState.CLOSED: {
        PipelineState.PATCH_PLANNING,
    },
    PipelineState.PATCH_PLANNING: {
        PipelineState.PATCH_SUCCESS,
        PipelineState.PATCH_FAILED,
    },
    PipelineState.PATCH_SUCCESS: set(),         # terminal
    PipelineState.PATCH_FAILED: set(),          # terminal
    PipelineState.CLOSURE_FORCED_FAIL: set(),   # terminal: budget-exhausted escape hatch
}

# ── Allowed subagent types per state ─────────────────────────────────────
# The orchestrator may only dispatch subagent types listed for the current state.

STATE_ACTIONS: dict[PipelineState, set[str]] = {
    PipelineState.UNDER_SPECIFIED: {"deep-search"},
    PipelineState.EVIDENCE_REFINING: {"closure-checker"},
    PipelineState.CLOSED: {"patch-planner"},
    PipelineState.PATCH_PLANNING: {"patch-generator"},
    PipelineState.PATCH_SUCCESS: set(),
    PipelineState.PATCH_FAILED: set(),
    PipelineState.CLOSURE_FORCED_FAIL: set(),
}


def is_valid_transition(from_state: PipelineState, to_state: PipelineState) -> bool:
    """Return True if transitioning from *from_state* to *to_state* is legal."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def is_allowed_action(state: PipelineState, subagent_type: str) -> bool:
    """Return True if *subagent_type* may be dispatched in *state*."""
    return subagent_type in STATE_ACTIONS.get(state, set())
