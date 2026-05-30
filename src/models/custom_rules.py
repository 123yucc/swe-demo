"""Custom-rule library models: tags, rules, and router output.

The custom-rule library is a small hand-curated set of repair-discipline
notes that lives outside the ChromaDB open-source corpus. Each rule is
tagged on three axes; the LLM router classifies the current
problem_statement on the same axes; multi-facet intersection decides
which rules to inject.

Design choices (see plan):
- Three axes: ``repo_type``, ``task_type``, ``change_shape``.
- On a rule's tags, ``None`` means the axis is a wildcard (always
  matches); a list means the rule applies only when the route output
  intersects that list.
- On the router output, every axis is a list. Empty list = "I cannot
  classify this case on this axis" (router was asked not to guess).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


RepoType = Literal[
    "web-app",
    "web-framework",
    "cli-tool",
    "library",
    "service-platform",
    "data-pipeline",
    "language-tooling",
]

TaskType = Literal[
    "auth-and-session",
    "data-access",
    "api-contract",
    "ui-display",
    "config-and-flags",
    "business-logic",
    "infra-integration",
    "test-and-tooling",
]

ChangeShape = Literal[
    "add-field",
    "add-method",
    "add-endpoint",
    "move-or-extract",
    "fix-validation",
    "fix-state-handling",
    "restructure",
    "rename",
    "config",
    "behavior-correction",
    "struct-shape-change",
]


class CustomRuleTags(BaseModel):
    """Tags attached to a custom rule for tag-tree routing.

    Each axis is either ``None`` (wildcard) or a non-empty list. A
    wildcard axis always matches; a list axis matches only when the
    router's output for that axis intersects it.
    """

    repo_type: list[RepoType] | None = Field(
        default=None,
        description=(
            "Allowed repo types for this rule. None means the rule "
            "applies regardless of repo type."
        ),
    )
    task_type: list[TaskType] | None = Field(
        default=None,
        description=(
            "Allowed task types for this rule. None means the rule "
            "applies regardless of task type."
        ),
    )
    change_shape: list[ChangeShape] | None = Field(
        default=None,
        description=(
            "Allowed change shapes for this rule. None means the rule "
            "applies regardless of change shape."
        ),
    )


class CustomRule(BaseModel):
    """One hand-written repair-discipline rule loaded from custom_knowledge.json."""

    id: str = Field(description="Stable id used for logging and dedup.")
    title: str = Field(description="One-line headline of the rule.")
    symptom: str = Field(
        default="",
        description=(
            "Human-readable description of the input shape this rule "
            "applies to. Not used for matching — kept for readability "
            "and to give the agent context when the rule is injected."
        ),
    )
    guidance: str = Field(
        description=(
            "The rule body — what the agent should do when this rule "
            "matches. Injected verbatim into working memory."
        ),
    )
    tags: CustomRuleTags = Field(
        default_factory=CustomRuleTags,
        description="Multi-facet tags used for tag-tree routing.",
    )


class RouteTags(BaseModel):
    """Router output: how the LLM classifies the current problem_statement.

    Every axis is a list. The router is asked to populate axes with the
    enum values that apply to this case, and to leave an axis as an
    empty list when it cannot confidently classify it.
    """

    repo_type: list[RepoType] = Field(
        default_factory=list,
        description=(
            "Repo types this case belongs to. Usually one value (a case "
            "lives in one repo); the list shape is for uniformity."
        ),
    )
    task_type: list[TaskType] = Field(
        default_factory=list,
        description=(
            "Task types this case touches. May span several values when "
            "the issue spans concerns (e.g. auth + ui)."
        ),
    )
    change_shape: list[ChangeShape] = Field(
        default_factory=list,
        description=(
            "Code-level change shapes this fix is likely to involve. "
            "May span several values."
        ),
    )
    rationale: str = Field(
        default="",
        description="Short justification for the classification.",
    )
