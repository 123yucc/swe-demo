"""
SharedWorkingMemory: the global shared context that all agents contribute to.

Aggregates issue context, evidence cards, cached code snippets, and a history
of orchestrator actions.  Local memory (subagent conversation history) is
implicit in the SDK — each subagent session is its own local memory.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.context import EvidenceCards
from src.models.evidence import RequirementItem
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
    ltm_search_summaries: list[str] = Field(
        default_factory=list,
        description=(
            "Progressive long-term-memory search summaries selected by code. "
            "Each entry is a compact outer-layer summary shown to the agent as "
            "candidate prior experience."
        ),
    )
    ltm_reference_block: str = Field(
        default="",
        description=(
            "Long-term-memory detail block (rendered text) containing browsed "
            "experience details. Populated only after code selects concrete "
            "experience ids to open. Empty when no detailed experience has "
            "been browsed yet."
        ),
    )
    custom_repair_block: str = Field(
        default="",
        description=(
            "Rendered text block of hand-written repair-discipline rules "
            "matched against the current case via the custom-rule tag "
            "router (see src/memory/custom_route.py). Independent of the "
            "ChromaDB-driven ltm_* fields above. Empty when no rule "
            "matches."
        ),
    )
    build_error_feedback: str = Field(
        default="",
        description=(
            "Rendered compile/collection errors from the post-patch build "
            "verification gate (src/orchestrator/build_verify.py). Set when "
            "the gate detects NEW build errors and the pipeline re-opens "
            "patch planning (repatch). Read by the patch-planner so the next "
            "plan fixes the broken build. Empty on the first planning pass."
        ),
    )
    evidence_focus_files: list[str] = Field(
        default_factory=list,
        description=(
            "Repatch-round evidence slicing (issue 010). When non-empty, "
            "format_for_prompt injects a SLICED evidence view instead of the "
            "full EvidenceCards dump: the top-level aggregate localization/"
            "structural/constraint cards (a redundant rebuilt view) are "
            "skipped, and only the RequirementItems whose evidence touches one "
            "of these files contribute their per-requirement scoped_evidence. "
            "The full repatch prompt was ~72k chars for navidrome — 56% of it "
            "the doubly-stored evidence — which pushed the planner SDK into the "
            "empty-structured_output failure mode. Empty = full-dump (first "
            "planning pass needs the global view; default behaviour unchanged)."
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
            recent_history = self.action_history[-20:]
            omitted = len(self.action_history) - len(recent_history)
            if omitted:
                lines.append(f"  ... {omitted} earlier events persisted on disk ...")
            for i, evt in enumerate(recent_history):
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

        ltm_section = ""
        if self.ltm_search_summaries:
            summaries = "\n".join(f"- {item}" for item in self.ltm_search_summaries)
            ltm_section += (
                "## Long-Term Memory Search Summaries\n"
                f"{summaries}\n\n"
            )
        if self.ltm_reference_block:
            ltm_section += f"{self.ltm_reference_block}\n\n"

        custom_section = ""
        if self.custom_repair_block:
            custom_section = (
                "## Custom Repair Discipline (matched by tag-route)\n"
                f"{self.custom_repair_block}\n\n"
            )

        build_section = ""
        if self.build_error_feedback:
            build_section = (
                "## Build Verification Errors to Fix\n"
                "The previous patch was applied but the tree FAILED to "
                "compile / collect. Fix these errors WITHOUT reverting the "
                "intended fix. Errors may implicate files that were not in the "
                "previous plan (e.g. a sibling call site of a renamed symbol, "
                "or a type that must be defined). Plan edits for every "
                "implicated file.\n"
                f"{self.build_error_feedback}\n\n"
            )

        evidence_section = self._render_evidence_section()

        return (
            "═══ SHARED WORKING MEMORY ═══\n\n"
            f"{ltm_section}"
            f"{custom_section}"
            f"{build_section}"
            f"{evidence_section}"
            f"{patch_section}"
            "## Retrieved Code Cache\n"
            f"{code_section}\n\n"
            "## Action History\n"
            f"{history_section}\n"
            "═══ END WORKING MEMORY ═══"
        )

    def format_for_deep_search(self, requirement: RequirementItem) -> str:
        """Render narrow non-evidence context for one investigation."""
        sections: list[str] = []
        if self.ltm_search_summaries:
            sections.append("## Relevant LTM summaries\n" + "\n".join(
                f"- {item}" for item in self.ltm_search_summaries
            ))
        if self.ltm_reference_block:
            sections.append(self.ltm_reference_block)
        if self.custom_repair_block:
            sections.append("## Custom repair rules\n" + self.custom_repair_block)
        if self.build_error_feedback:
            sections.append("## Relevant build feedback\n" + self.build_error_feedback)
        related = [
            event for event in self.action_history
            if not event.requirement_id or event.requirement_id == requirement.id
        ][-12:]
        if related:
            sections.append("## Recent action summary\n" + "\n".join(
                f"- {e.phase}|{e.subagent or '-'}|{e.requirement_id or '-'}|{e.outcome}"
                for e in related
            ))
        return "\n\n".join(sections)

    def _render_evidence_section(self) -> str:
        """Render the evidence-cards block, full or sliced.

        Full dump (``evidence_focus_files`` empty): the entire EvidenceCards
        JSON minus ``requirement_status``. This is the first-pass behaviour —
        the planner needs the global aggregate view to plan from scratch.

        Sliced (``evidence_focus_files`` non-empty, set on a repatch round):
        the top-level aggregate localization/structural/constraint cards are
        SKIPPED — they are a redundant rebuild of the per-requirement slices
        and account for ~half of a large evidence dump (issue 010). Only the
        RequirementItems whose own evidence touches a focus file are emitted,
        each carrying its scoped_evidence. The parser-owned, globally-scoped
        symptom card is small and always kept.
        """
        if not self.evidence_focus_files:
            return (
                "## Evidence Cards (active repair context)\n"
                f"```json\n{self.evidence_cards.model_dump_json(indent=2, exclude={'requirement_status'})}\n```\n\n"
            )

        focus = [f.replace("\\", "/").strip().lstrip("./")
                 for f in self.evidence_focus_files]

        def _req_touches_focus(req: RequirementItem) -> bool:
            # Serialize the requirement's own evidence (locations + scoped
            # cards) and check whether any focus file path appears in it.
            blob = "\n".join(req.evidence_locations)
            blob += "\n" + req.scoped_evidence.model_dump_json()
            blob = blob.replace("\\", "/")
            return any(f in blob for f in focus)

        kept = [r for r in self.evidence_cards.requirements
                if _req_touches_focus(r)]
        # Fallback: if slicing matched nothing (e.g. errors only in test files
        # the requirements never cited), keep all active requirements rather
        # than emit an empty evidence section that strands the planner.
        if not kept:
            kept = list(self.evidence_cards.requirements)

        symptom_json = self.evidence_cards.symptom.model_dump_json(indent=2)
        reqs_json = "[\n" + ",\n".join(
            r.model_dump_json(indent=2) for r in kept
        ) + "\n]"
        return (
            "## Evidence Cards (SLICED to files implicated this repatch round)\n"
            "Only the requirements whose evidence touches the files that "
            "failed to build are shown, each with its per-requirement scoped "
            "evidence. The aggregate localization/structural/constraint cards "
            "are omitted this round — work from the scoped evidence below and "
            "the build errors above.\n"
            f"### symptom\n```json\n{symptom_json}\n```\n"
            f"### requirements (sliced)\n```json\n{reqs_json}\n```\n\n"
        )

