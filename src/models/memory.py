"""
SharedWorkingMemory: the global shared context that all agents contribute to.

Aggregates issue context, evidence cards, cached code snippets, and a history
of orchestrator actions.  Local memory (subagent conversation history) is
implicit in the SDK — each subagent session is its own local memory.
"""

from pydantic import BaseModel, Field

from src.models.context import EvidenceCards


class SharedWorkingMemory(BaseModel):
    """Global working memory shared across the orchestrator and all sub-agents.

    Fields:
        issue_context:  Original issue artifact text (immutable after init).
        evidence_cards: The four evidence cards — sole Source of Truth.
        retrieved_code: Cached code snippets keyed by "filepath:start-end".
        action_history: Chronological log of orchestrator actions taken.
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
    retrieved_code: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Cached code snippets. Key format: 'filepath:start_line-end_line', "
            "value: the actual source code text. Deep Search agents populate "
            "this via the cache_retrieved_code tool when they find critical code."
        ),
    )
    action_history: list[str] = Field(
        default_factory=list,
        description=(
            "Chronological log of orchestrator actions: which Deep Search "
            "tasks were dispatched, what evidence was persisted, etc."
        ),
    )

    def record_action(self, action: str) -> None:
        """Append an action to the history log."""
        self.action_history.append(action)

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
            history_section = "\n".join(
                f"  {i+1}. {a}" for i, a in enumerate(self.action_history)
            )
        else:
            history_section = "(no actions yet)"

        return (
            "═══ SHARED WORKING MEMORY ═══\n\n"
            "## Evidence Cards (current state)\n"
            f"```json\n{self.evidence_cards.model_dump_json(indent=2)}\n```\n\n"
            "## Retrieved Code Cache\n"
            f"{code_section}\n\n"
            "## Action History\n"
            f"{history_section}\n"
            "═══ END WORKING MEMORY ═══"
        )
