"""
Patch planning and generation models.

PatchPlan is produced by the Patch Planner agent and consumed by the
Patch Generator agent.  It describes *what* to change and *why*, without
containing actual code edits.

Phase 18.D added preserved_findings to FileEditPlan — the patch-planner must
copy prescriptive findings原文 to ensure boundary constraints reach the
patch-generator without loss.
"""

from pydantic import BaseModel, Field


class FileEditPlan(BaseModel):
    """A single file-level edit intent within the overall patch plan."""

    filepath: str = Field(
        description=(
            "Path to the file to be modified, relative to the repository root."
        ),
    )
    target_functions: list[str] = Field(
        default_factory=list,
        description=(
            "Functions, methods, or classes inside this file that need to be "
            "modified or added."
        ),
    )
    change_rationale: str = Field(
        description=(
            "Why this file needs to change — references the evidence cards "
            "(e.g. which exact_code_region, which constraint, which co-edit "
            "relation) that justify this edit."
        ),
    )
    preserved_findings: list[str] = Field(
        default_factory=list,
        description=(
            "Original prescriptive snippets from RequirementItem.findings that "
            "apply to this file.  Patch-planner MUST copy these verbatim — "
            "no summarization or paraphrasing.  Examples: backtick code "
            "snippets, 'correct form is X', 'must use Y', explicit boundary "
            "constraints.  These are hard constraints the patch-generator must "
            "respect."
        ),
    )
    co_edit_dependencies: list[str] = Field(
        default_factory=list,
        description=(
            "Other filepaths that must be edited together with this file "
            "to keep the codebase consistent (derived from "
            "StructuralCard.must_co_edit_relations)."
        ),
    )


class PatchPlan(BaseModel):
    """Strategic edit plan produced by the Patch Planner agent.

    Contains a high-level overview and per-file edit intents.  Does NOT
    contain actual code — that is the Patch Generator's job.
    """

    overview: str = Field(
        description=(
            "High-level summary of the fix strategy: what the root cause is, "
            "what approach is taken, and how it respects the constraints."
        ),
    )
    edits: list[FileEditPlan] = Field(
        description="Ordered list of per-file edit plans.",
    )
