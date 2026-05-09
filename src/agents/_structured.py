"""Structured output helper backed by SDK native output_format.

Thin wrapper around `query()` + `ClaudeAgentOptions.output_format` that
returns a pydantic-validated instance of `response_model`. The SDK handles
constrained decoding and validation retries; on success the validated dict
arrives as `ResultMessage.structured_output`.

On `error_max_structured_output_retries` (or missing structured_output) the
helper raises RuntimeError so the orchestrator can record the failure and
move on — there is no manual JSON-extraction fallback.
"""

from __future__ import annotations

from typing import TypeVar

import src.config  # noqa: F401  — side-effect: load .env into os.environ

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


async def run_structured_query(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    component: str,
    allowed_tools: list[str] | None = None,
    max_turns: int = 10,
    max_budget_usd: float = 1.0,
    permission_mode: str = "acceptEdits",
    cwd: str | None = None,
) -> T:
    """Run a query and return a validated instance of *response_model*.

    Args:
        system_prompt:  System message.
        user_prompt:    User message.
        response_model: Pydantic model whose JSON Schema the SDK enforces.
        component:      Short name used in error messages.
        allowed_tools:  SDK tool allowlist (e.g. ["Grep", "Read", "Glob"]).
        max_turns:      Per-query SDK tool-turn cap.
        max_budget_usd: Per-query SDK USD cap.
        permission_mode: SDK permission mode (default "acceptEdits").
        cwd:            Working directory for tool execution.
    """
    options_kwargs: dict = dict(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or [],
        permission_mode=permission_mode,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format={
            "type": "json_schema",
            "schema": response_model.model_json_schema(),
        },
    )
    if cwd is not None:
        options_kwargs["cwd"] = cwd
    options = ClaudeAgentOptions(**options_kwargs)

    result_message: ResultMessage | None = None
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, ResultMessage):
            result_message = message

    if result_message is None:
        raise RuntimeError(f"{component}: SDK returned no ResultMessage.")

    if result_message.subtype in ("error_max_turns", "error_max_budget_usd"):
        raise RuntimeError(
            f"{component}: aborted due to per-query limit "
            f"({result_message.subtype})."
        )
    if result_message.subtype == "error_max_structured_output_retries":
        raise RuntimeError(
            f"{component}: SDK exhausted structured-output retries."
        )

    structured = result_message.structured_output
    if structured is None:
        raise RuntimeError(
            f"{component}: SDK returned no structured_output "
            f"(subtype={result_message.subtype})."
        )

    return response_model.model_validate(structured)
