"""LLM router for the custom-rule library.

Classifies the current ``problem_statement`` on three axes
(``repo_type`` / ``task_type`` / ``change_shape``) and returns a
``RouteTags`` instance. Reuses ``run_structured_query`` so the call
inherits the rest of the codebase's SDK setup (model from
``ANTHROPIC_MODEL``, structured output, retries) — no manual JSON
parsing or HTTP fallback.
"""

from __future__ import annotations

from src.agents._structured import run_structured_query
from src.models.custom_rules import (
    ChangeShape,
    RepoType,
    RouteTags,
    TaskType,
)


def _enum_block(label: str, values: tuple[str, ...]) -> str:
    return f"{label}: {', '.join(values)}"


_ROUTER_SYSTEM_PROMPT = f"""\
You are a Long-Term-Memory Routing Agent. Read the user's bug
``problem_statement`` and classify it on three axes used by the custom
repair-discipline library.

Output a JSON object with exactly these fields:
- repo_type: list of values from the enum below (usually one).
- task_type: list of values from the enum below.
- change_shape: list of values from the enum below.
- rationale: one short sentence justifying the classification.

Enum values:
- {_enum_block("repo_type", RepoType.__args__)}
- {_enum_block("task_type", TaskType.__args__)}
- {_enum_block("change_shape", ChangeShape.__args__)}

Rules:
1. Use ONLY the enum values listed above. Do not invent new values.
2. If you are genuinely uncertain about an axis, return an empty list
   for that axis. Do NOT guess. Empty is better than wrong.
3. Multiple values are allowed on task_type and change_shape; one case
   often spans several. repo_type is usually one value.
4. Classify the case as a whole — do not enumerate every possible tag,
   only the ones actually supported by the problem_statement.
5. ``change_shape`` clarifications:
   - ``rename`` is for renaming or visibility-flipping NAMED symbols
     (a function, a method, a type, a struct field, a config key).
     Add this tag whenever the patch is expected to rewrite call
     sites or references to old names — including when the renames
     happen as part of a larger struct/class shape rewrite.
   - ``struct-shape-change`` is for cases that rewrite the field set
     of a struct/class as a whole — adding, removing, or renaming
     several fields, removing/introducing embedded (anonymous) fields,
     reordering or recategorizing constructor parameters. The signal
     is that the requirement enumerates a target field/parameter set
     rather than naming one or two callers. Add this tag whenever the
     fix shape is "the struct's exposed fields look like this now".
   - ``struct-shape-change`` and ``rename`` are NOT mutually exclusive.
     A struct rewrite that renames fields gets BOTH tags. A struct
     rewrite that only adds new fields gets ``struct-shape-change``
     and ``add-field``. A symbol rename that does not touch a struct
     definition gets only ``rename``.
"""


async def run_custom_router(
    problem_statement: str,
    *,
    cwd: str | None = None,
) -> RouteTags:
    """Classify ``problem_statement`` and return ``RouteTags``.

    Empty/whitespace input short-circuits to an empty ``RouteTags``
    without spending a turn.
    """
    if not problem_statement.strip():
        return RouteTags()

    return await run_structured_query(
        system_prompt=_ROUTER_SYSTEM_PROMPT,
        user_prompt=problem_statement,
        response_model=RouteTags,
        component="custom-router",
        allowed_tools=[],
        max_turns=4,
        max_budget_usd=0.1,
        cwd=cwd,
    )
