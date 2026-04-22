"""
SharedWorkingMemory: the global shared context that all agents contribute to.

Aggregates issue context, evidence cards, cached code snippets, and a history
of orchestrator actions.  Local memory (subagent conversation history) is
implicit in the SDK — each subagent session is its own local memory.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.context import EvidenceCards
from src.models.patch import PatchPlan


class ActionEvent(BaseModel):
    """One aggregated orchestrator event.

    Only records cross-agent aggregate dimensions.  Per-message detail for a
    single query() is already persisted by the SDK under
    ``~/.claude/projects/<encoded-cwd>/*.jsonl``; duplicating it here would
    waste space and drift.
    """

    phase: str = Field(
        description="Pipeline phase, e.g. 'parser', 'deep-search', 'closure-checker'.",
    )
    subagent: str = Field(
        default="",
        description="Concrete sub-agent invoked, if any.",
    )
    outcome: str = Field(
        description=(
            "Short outcome label (APPROVED / EVIDENCE_MISSING / PATCH_SUCCESS "
            "/ budget_exhausted / ...)."
        ),
    )
    requirement_id: str = Field(
        default="",
        description="RequirementItem.id this event is scoped to, if any.",
    )


class SharedWorkingMemory(BaseModel):
    """Global working memory shared across the orchestrator and all sub-agents.

    Fields:
        issue_context:  Original issue artifact text (immutable after init).
        evidence_cards: The four evidence cards — sole Source of Truth.
        retrieved_code: Cached code snippets keyed by "filepath:start-end".
        action_history: Chronological structured log of orchestrator events.
    """

    issue_context: str = Field(
        description=(
            "Concatenated text of all issue artifact documents. "
            "Set once at initialization and never modified."
        ),
    )
    evidence_cards: EvidenceCards = Field(
        description="Current state of the four evidence cards.",
    )
    patch_plan: PatchPlan | None = Field(
        default=None,
        description=(
            "Structured edit plan produced by the Patch Planner agent. "
            "None until the planner has run."
        ),
    )
    retrieved_code: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Cached code snippets. Key format: 'filepath:start_line-end_line', "
            "value: the actual source code text. Deep Search agents populate "
            "this via the cache_retrieved_code tool when they find critical code."
        ),
    )
    action_history: list[ActionEvent] = Field(
        default_factory=list,
        description=(
            "Chronological log of orchestrator events (phase, subagent, "
            "outcome, requirement_id). Per-message detail lives in SDK "
            "session files; this list holds only aggregate dimensions."
        ),
    )

    def record_action(
        self,
        phase: str,
        outcome: str,
        *,
        subagent: str = "",
        requirement_id: str = "",
    ) -> ActionEvent:
        """Append a structured event to the history log.

        Returns the event that was appended so callers can chain if needed.
        """
        event = ActionEvent(
            phase=phase,
            subagent=subagent,
            outcome=outcome,
            requirement_id=requirement_id,
        )
        self.action_history.append(event)
        return event

    def format_for_prompt(self) -> str:
        """Render the working memory as a structured text block suitable for
        injection into a system prompt or user message."""
        code_section = ""
        if self.retrieved_code:
            snippets = []
            for key, code in self.retrieved_code.items():
                snippets.append(f"### {key}\n```\n{code}\n```")
            code_section = "\n\n".join(snippets)
        else:
            code_section = "(none cached yet)"

        history_section = ""
        if self.action_history:
            lines: list[str] = []
            for i, evt in enumerate(self.action_history):
                parts = [f"phase={evt.phase}"]
                if evt.subagent:
                    parts.append(f"subagent={evt.subagent}")
                if evt.requirement_id:
                    parts.append(f"req={evt.requirement_id}")
                parts.append(f"outcome={evt.outcome}")
                lines.append(f"  {i+1}. " + ", ".join(parts))
            history_section = "\n".join(lines)
        else:
            history_section = "(no actions yet)"

        patch_section = ""
        if self.patch_plan is not None:
            patch_section = (
                "## Patch Plan\n"
                f"```json\n{self.patch_plan.model_dump_json(indent=2)}\n```\n\n"
            )

        return (
            "═══ SHARED WORKING MEMORY ═══\n\n"
            "## Evidence Cards (current state)\n"
            f"```json\n{self.evidence_cards.model_dump_json(indent=2)}\n```\n\n"
            f"{patch_section}"
            "## Retrieved Code Cache\n"
            f"{code_section}\n\n"
            "## Action History\n"
            f"{history_section}\n"
            "═══ END WORKING MEMORY ═══"
        )
