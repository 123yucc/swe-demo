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

import asyncio
import os
import uuid
from dataclasses import replace
from typing import TypeVar

import src.config  # noqa: F401 - side-effect: load .env into os.environ

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query
from pydantic import BaseModel

from src.agents._backend import model_backend, use_openai_backend
from src.agents import _cost_tracker
from src.agents.call_metrics import CallRecorder, current as current_call_metrics, model_label
from src.agents._cost_policy import anthropic_hooks

T = TypeVar("T", bound=BaseModel)
_CLAUDE_CLIENTS: dict[tuple, ClaudeSDKClient] = {}
_CLAUDE_CLIENT_TOTALS: dict[tuple, tuple[float, dict[str, int]]] = {}


async def close_structured_clients() -> None:
    """Close persistent Claude transports before their event loop exits."""
    clients = list(_CLAUDE_CLIENTS.values())
    _CLAUDE_CLIENTS.clear()
    _CLAUDE_CLIENT_TOTALS.clear()
    for client in clients:
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            # Some Claude SDK/AnyIO versions cancel their internal write scope
            # during a normal disconnect. The model result is already complete.
            pass
        except Exception:
            pass


async def _claude_client_query(
    *, options: ClaudeAgentOptions, prompt: str, cache_key: tuple,
) -> ResultMessage | None:
    """Reuse one CLI transport while isolating each call by session id."""
    client = _CLAUDE_CLIENTS.get(cache_key)
    if client is None:
        client = ClaudeSDKClient(options=options)
        await client.connect()
        _CLAUDE_CLIENTS[cache_key] = client
    try:
        await client.query(prompt, session_id=uuid.uuid4().hex)
        result_message = None
        async for message in client.receive_response():
            metrics = current_call_metrics()
            if metrics:
                metrics.turn()
            if isinstance(message, ResultMessage):
                result_message = message
        if result_message is None:
            return None
        previous_cost, previous_usage = _CLAUDE_CLIENT_TOTALS.get(cache_key, (0.0, {}))
        raw_cost = float(result_message.total_cost_usd or 0.0)
        raw_usage = dict(result_message.usage or {})
        delta_usage = {
            key: max(0, int(value or 0) - int(previous_usage.get(key, 0) or 0))
            for key, value in raw_usage.items()
            if isinstance(value, (int, float))
        }
        _CLAUDE_CLIENT_TOTALS[cache_key] = (raw_cost, {
            key: int(value or 0) for key, value in raw_usage.items()
            if isinstance(value, (int, float))
        })
        return replace(
            result_message,
            total_cost_usd=max(0.0, raw_cost - previous_cost),
            usage={**raw_usage, **delta_usage},
        )
    except Exception:
        _CLAUDE_CLIENTS.pop(cache_key, None)
        _CLAUDE_CLIENT_TOTALS.pop(cache_key, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        raise


async def _run_structured_query_impl(
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
        if os.environ.get("OPENAI_AGENT_LOOP", "native").strip().lower() == "agents_sdk":
            from src.agents._openai_agents_sdk import run_agents_structured_query
            return await run_agents_structured_query(
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

    from src.agents._anthropic_native import (
        enabled as anthropic_native_enabled,
        run_anthropic_structured_query,
    )
    if anthropic_native_enabled(allowed_tools):
        return await run_anthropic_structured_query(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            component=component,
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
    hooks = anthropic_hooks()
    if hooks:
        base_options_kwargs["hooks"] = hooks
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
        metrics = current_call_metrics()
        if metrics:
            metrics.retry(attempt)
        options = ClaudeAgentOptions(**base_options_kwargs)
        prompt = user_prompt if attempt == 1 else (user_prompt + retry_nudge)

        if os.environ.get("CLAUDE_SDK_TRANSPORT", "query").strip().lower() == "client":
            cache_key = (
                component, system_prompt, tuple(allowed_tools or []), cwd,
                response_model.__name__, max_turns, max_budget_usd,
            )
            result_message = await _claude_client_query(
                options=options, prompt=prompt, cache_key=cache_key,
            )
        else:
            result_message = None
            async for message in query(prompt=prompt, options=options):
                if metrics:
                    metrics.turn()
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


async def run_structured_query(
    *, system_prompt: str, user_prompt: str, response_model: type[T],
    component: str, allowed_tools: list[str] | None = None,
    max_turns: int = 10, max_budget_usd: float = 1.0,
    permission_mode: str = "acceptEdits", cwd: str | None = None,
    max_attempts: int = 2, allow_none: bool = False,
    call_reason: str = "", requirement_ids: list[str] | None = None,
) -> T | None:
    """Instrumented public entry point shared by both model backends."""
    with CallRecorder(
        component=component,
        model=f"{model_backend()}:{model_label()}",
        prompt=system_prompt + user_prompt,
        call_reason=call_reason,
        requirement_ids=requirement_ids,
    ):
        return await _run_structured_query_impl(
            system_prompt=system_prompt, user_prompt=user_prompt,
            response_model=response_model, component=component,
            allowed_tools=allowed_tools, max_turns=max_turns,
            max_budget_usd=max_budget_usd, permission_mode=permission_mode,
            cwd=cwd, max_attempts=max_attempts, allow_none=allow_none,
        )
