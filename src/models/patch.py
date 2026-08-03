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
    reference_only: bool = Field(
        default=False,
        description=(
            "True when this edit was auto-added by planner backfill from a "
            "co-edit relation rather than chosen by the planner as a "
            "definite change target.  Such files are frequently referenced "
            "in evidence only as read-for-pattern context (e.g. 'read "
            "user.js to learn the privilege-check pattern'), not as files "
            "that must change.  The patch-generator treats a no-op outcome "
            "on a reference_only edit as acceptable (NO_OP_OK) instead of "
            "FAILED, so a backfilled false-positive does not sink an "
            "otherwise-complete patch."
        ),
    )
    expected_diff_required: bool = Field(
        default=True,
        description=(
            "True when this planned edit is expected to produce a concrete "
            "change in patch.diff. reference_only edits may set this false; "
            "all other edits default to true so the artifact verifier can "
            "catch plan/diff drift before evaluation."
        ),
    )
    creates_new_file: bool = Field(
        default=False,
        description=(
            "True when this edit intentionally creates filepath. The artifact "
            "verifier treats the file's existence and diff presence as hard "
            "requirements."
        ),
    )
    expected_symbols: list[str] = Field(
        default_factory=list,
        description=(
            "Symbols this edit is expected to define in filepath, such as a "
            "new function, type, class, or exported constant. Used by the "
            "artifact verifier to catch missing-method/missing-type patches."
        ),
    )
    required_by_requirement_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Requirement ids that make this edit mandatory. Empty means the "
            "planner did not provide requirement-level provenance."
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
