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
    max_attempts: int = 2,
    allow_none: bool = False,
) -> T | None:
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
        max_attempts:   How many times to run the query when the SDK returns
                        ``subtype=="success"`` but no ``structured_output``.
                        This is a transient failure mode (the agent finishes
                        but emits a final text turn instead of the structured
                        result) seen on large prompts; a single re-run almost
                        always recovers it. Re-runs append an explicit nudge
                        to emit ONLY the structured object.
        allow_none:     When True, exhausting ``max_attempts`` on the empty
                        structured_output path returns ``None`` instead of
                        raising. The caller is then responsible for degrading
                        gracefully (e.g. a repatch planner that falls back to
                        BUILD_FAILED rather than crashing the whole run —
                        issue 010). Hard failures (no ResultMessage, per-query
                        limit hit, exhausted SDK retries) still raise regardless
                        — those are not the recoverable "empty output" mode.
    """

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

    # On a retry, steer the agent away from the failure mode that produced no
    # structured output: a final free-text turn instead of the JSON result.
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

        # subtype is (almost certainly) "success" but no structured_output —
        # the transient mode. Retry if we have budget left.
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
