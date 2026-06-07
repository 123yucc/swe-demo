from typing import Literal

from pydantic import BaseModel, Field


RequirementVerdict = Literal[
    "UNCHECKED",
    "AS_IS_COMPLIANT",
    "AS_IS_VIOLATED",
    "TO_BE_MISSING",
    "TO_BE_PARTIAL",
]

# Subset used by deep-search output — UNCHECKED is not a valid investigation result.
ActiveVerdict = Literal[
    "AS_IS_COMPLIANT",
    "AS_IS_VIOLATED",
    "TO_BE_MISSING",
    "TO_BE_PARTIAL",
]

RequirementOrigin = Literal["problem_statement", "requirements", "new_interfaces"]


class RequirementStatus(BaseModel):
    """Lightweight coverage record for requirements that need no repair.

    Compliant requirements are intentionally kept out of ``requirements`` once
    verified so downstream patch agents consume a focused active repair queue.
    """

    id: str = Field(
        description="Stable identifier of the original RequirementItem.",
    )
    text: str = Field(
        description="Original requirement text.",
    )
    origin: RequirementOrigin = Field(
        description="Original requirement origin.",
    )
    verdict: Literal["AS_IS_COMPLIANT"] = Field(
        default="AS_IS_COMPLIANT",
        description="Only compliant requirements are stored here.",
    )
    short_reason: str = Field(
        default="",
        description="Short verified reason; do not store long findings here.",
    )
    evidence_locations: list[str] = Field(
        default_factory=list,
        description="Verified code locations supporting the compliant status.",
    )


class SymptomCard(BaseModel):
    """Describes the observable failure symptoms and repair expectations."""

    observable_failures: list[str] = Field(
        default_factory=list,
        description=(
            "Visible symptoms extracted from the issue: error messages, "
            "exception types, stack traces, and other observable anomalies."
        ),
    )
    repair_targets: list[str] = Field(
        default_factory=list,
        description=(
            "What the fix should achieve — the expected behaviour once the "
            "defect is resolved."
        ),
    )
    regression_expectations: list[str] = Field(
        default_factory=list,
        description=(
            "Existing correct behaviours that MUST NOT be broken by the fix "
            "(regression guardrails)."
        ),
    )


class LocalizationCard(BaseModel):
    """Points to where in the codebase the bug likely lives, with
    program-analysis context (call chains, data flow)."""

    suspect_entities: list[str] = Field(
        default_factory=list,
        description=(
            "Suspected files, classes, functions, or variables involved in "
            "the defect."
        ),
    )
    exact_code_regions: list[str] = Field(
        default_factory=list,
        description=(
            "Exact code lines or hunks confirmed to contain the defect "
            "(e.g. 'auth.py:42-58')."
        ),
    )
    call_chain_context: list[str] = Field(
        default_factory=list,
        description=(
            "Call chains around the defect location — Caller-Callee "
            "relationships that explain how the buggy code is reached."
        ),
    )
    dataflow_relevant_uses: list[str] = Field(
        default_factory=list,
        description=(
            "Relevant variable definitions and their use sites (Def-Use "
            "relationships) that influence or are influenced by the defect."
        ),
    )


class ConstraintCard(BaseModel):
    """Captures constraints and reference patterns the fix must respect."""

    semantic_boundaries: list[str] = Field(
        default_factory=list,
        description=(
            "API contracts, docstring / annotation constraints, and other "
            "semantic boundaries the fix must not violate."
        ),
    )
    behavioral_constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Assertions, invariants, explicit schema constraints, and other "
            "behavioural rules enforced by the codebase."
        ),
    )
    backward_compatibility: list[str] = Field(
        default_factory=list,
        description="Backward-compatibility requirements the fix must preserve.",
    )
    similar_implementation_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Existing similar API implementations in the codebase that serve "
            "as reference baselines for how the fix should be structured."
        ),
    )
    missing_elements_to_implement: list[str] = Field(
        default_factory=list,
        description=(
            "Elements required by specifications but entirely absent from the "
            "current codebase (interfaces, classes, methods). Explicitly listed "
            "to prevent downstream agents from hallucinating they already exist."
        ),
    )


class StructuralCard(BaseModel):
    """Describes co-edit dependencies and propagation paths."""

    must_co_edit_relations: list[str] = Field(
        default_factory=list,
        description=(
            "Co-edit dependencies: if location A is modified, location B must "
            "also be updated (e.g. interface A → all callers of A)."
        ),
    )
    dependency_propagation: list[str] = Field(
        default_factory=list,
        description=(
            "Cross-cutting dependency paths: interface / package / config "
            "relationships that propagate changes across the codebase."
        ),
    )
    consistency_anchors: list[str] = Field(
        default_factory=list,
        description=(
            "Pairs of code points that MUST stay jointly consistent. "
            "Format: '<endpoint_a> <-> <endpoint_b>' where each endpoint is "
            "either 'path/to/file.ext:LINE', 'path/to/file.ext:LINE-LINE', "
            "or 'path/to/file.ext:symbol:NAME' (symbol prefix may be 'class', "
            "'func', 'type', 'enum', 'field', 'method', or 'name'). "
            "Use this for: (a) configuration-file references that must have a "
            "code-side definition, (b) renamed/visibility-changed symbols and "
            "all their grep-visible callers (including same-package _test.* "
            "files at base_commit), (c) new files and their mount/import sites. "
            "Every anchor is machine-verified by the consistency code gate; "
            "agent-invisible files (e.g. evaluator-injected hidden tests) are "
            "out of scope and must not be referenced."
        ),
    )


class ScopedEvidence(BaseModel):
    """One requirement's own slice of the three deep-search-owned cards.

    This is the per-requirement SOURCE of all deep-search code observations;
    the top-level EvidenceCards.localization / structural / constraint cards
    are the DERIVED aggregate view rebuilt from every requirement's slice.

    Only the three deep-search-owned cards are mirrored here. SymptomCard is
    parser-owned and global (no per-requirement scope), so it is deliberately
    absent. constraint.missing_elements_to_implement is parser-owned too and
    stays empty here — it lives only on the top-level aggregate ConstraintCard.
    """

    localization: LocalizationCard = Field(
        default_factory=LocalizationCard,
        description="This requirement's localization observations.",
    )
    structural: StructuralCard = Field(
        default_factory=StructuralCard,
        description="This requirement's structural / co-edit observations.",
    )
    constraint: ConstraintCard = Field(
        default_factory=ConstraintCard,
        description=(
            "This requirement's constraint observations (deep-search-owned "
            "subset only; missing_elements_to_implement stays empty here)."
        ),
    )


class RequirementItem(BaseModel):
    """One behavioral requirement extracted from the issue, tracked with a verdict.

    RequirementItem is the task-driving unit across the whole pipeline.
    Parser initializes each item with verdict=UNCHECKED; deep-search updates
    verdict / evidence_locations / findings by calling update_requirement_verdict.
    """

    id: str = Field(
        description="Stable identifier in the form 'req-001', 'req-002', ...",
    )
    text: str = Field(
        description=(
            "Original requirement text — do NOT truncate or paraphrase."
        ),
    )
    origin: RequirementOrigin = Field(
        description=(
            "Which input section this requirement came from: "
            "'problem_statement', 'requirements', or 'new_interfaces'."
        ),
    )
    verdict: RequirementVerdict = Field(
        default="UNCHECKED",
        description=(
            "Deep-search's verdict for this requirement after on-site verification. "
            "UNCHECKED until deep-search has investigated it."
        ),
    )
    evidence_locations: list[str] = Field(
        default_factory=list,
        description=(
            "Code locations (e.g. 'path/to/file.py:LINE' or 'path/to/file.py:LINE-LINE') "
            "that substantiate the verdict. Required to be non-empty unless "
            "verdict == 'AS_IS_COMPLIANT'."
        ),
    )
    findings: str = Field(
        default="",
        description=(
            "Deep-search's on-site verification summary — what the code actually "
            "says about this requirement. Used by downstream patch agents."
        ),
    )
    scoped_evidence: ScopedEvidence = Field(
        default_factory=ScopedEvidence,
        description=(
            "This requirement's own slice of the deep-search-owned cards "
            "(localization / structural / constraint). Written by "
            "update_localization scoped to this requirement id; the top-level "
            "EvidenceCards aggregate cards are rebuilt from every "
            "requirement's slice. This is the persisted SOURCE that preserves "
            "per-requirement attribution lost in the aggregate view."
        ),
    )
    rework_context: str = Field(
        default="",
        description=(
            "Closure-checker's per-requirement audit feedback written here when "
            "the requirement is re-opened for a rework cycle. Non-empty means "
            "deep-search should read this and give a reasoning path different "
            "from the previous verdict. Cleared automatically after the next "
            "deep-search verdict is persisted."
        ),
    )
