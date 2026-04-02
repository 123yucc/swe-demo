"""Hard-check router for phase transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .state_machine import LoopState, can_transition


@dataclass
class RoutingCheck:
    allowed: bool
    target_state: LoopState
    reason: str = ""


class PhaseRouter:
    """Only enforce hard constraints, do not make strategy decisions."""

    def check(self, current: LoopState, requested: LoopState, has_patch_output: bool, closure_passed: bool) -> RoutingCheck:
        if not can_transition(current, requested):
            return RoutingCheck(False, current, f"illegal transition: {current.value} -> {requested.value}")

        if requested == LoopState.CLOSED and not closure_passed:
            return RoutingCheck(False, current, "closure not satisfied")

        if requested in {LoopState.PATCH_FAILED, LoopState.PATCH_SUCCESS} and not has_patch_output:
            return RoutingCheck(False, current, "patch output missing")

        return RoutingCheck(True, requested, "ok")
