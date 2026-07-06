"""Structured output helper backed by SDK native output_format.

Thin wrapper around `query()` + `ClaudeAgentOptions.output_format` that
returns a pydantic-validated instance of `response_model`. The SDK handles
constrained decoding and validation retries; on success the validated dict
arrives as `ResultMessage.structured_output`.

On `error_max_structured_output_retries` (or missing structured_output) the
helper raises RuntimeError so the orchestrator can record the failure and
move on.
"""

from __future__ import annotations

from typing import TypeVar

import src.config  # noqa: F401 - side-effect: load .env into os.environ

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel

from src.agents._backend import use_openai_backend
from src.agents import _cost_tracker

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
    max_attempts: int = 2,
    allow_none: bool = False,
) -> T | None:
    """Run a query and return a validated instance of *response_model*."""
    if use_openai_backend():
        from src.agents._openai_native import run_openai_structured_query

        return await run_openai_structured_query(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            component=component,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            cwd=cwd,
            max_attempts=max_attempts,
            allow_none=allow_none,
        )

    base_options_kwargs: dict = dict(
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
        base_options_kwargs["cwd"] = cwd

    retry_nudge = (
        "\n\nIMPORTANT: Do NOT end with an explanatory text message. Your "
        "final action MUST be to emit the structured output object that "
        "matches the required JSON schema. Stop tool use once you have "
        "enough information and return the structured result directly."
    )

    last_subtype: str | None = None
    for attempt in range(1, max_attempts + 1):
        options = ClaudeAgentOptions(**base_options_kwargs)
        prompt = user_prompt if attempt == 1 else (user_prompt + retry_nudge)

        result_message: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result_message = message

        if result_message is None:
            raise RuntimeError(f"{component}: SDK returned no ResultMessage.")

        _cost_tracker.accumulate(result_message)

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
        if structured is not None:
            return response_model.model_validate(structured)

        last_subtype = result_message.subtype
        if attempt < max_attempts:
            print(
                f"[{component}] SDK returned no structured_output "
                f"(subtype={last_subtype}); retrying "
                f"(attempt {attempt + 1}/{max_attempts}).",
                flush=True,
            )

    if allow_none:
        print(
            f"[{component}] SDK returned no structured_output after "
            f"{max_attempts} attempt(s) (subtype={last_subtype}); "
            "returning None for caller-side graceful degradation.",
            flush=True,
        )
        return None
    raise RuntimeError(
        f"{component}: SDK returned no structured_output after "
        f"{max_attempts} attempt(s) (subtype={last_subtype})."
    )
