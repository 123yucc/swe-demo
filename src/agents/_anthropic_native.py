"""Native Anthropic path for tool-free structured calls with explicit caching."""

from __future__ import annotations

import os
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from src.agents import _cost_tracker

T = TypeVar("T", bound=BaseModel)


def enabled(allowed_tools: list[str] | None) -> bool:
    mode = os.environ.get("ANTHROPIC_API_MODE", "agent_sdk").strip().lower()
    return mode in {"hybrid", "native_structured"} and not allowed_tools


async def run_anthropic_structured_query(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    component: str,
    max_attempts: int,
    allow_none: bool,
) -> T | None:
    client = AsyncAnthropic()
    schema = response_model.model_json_schema()
    last_error = "no response"
    for attempt in range(1, max_attempts + 1):
        prompt = user_prompt
        if attempt > 1:
            prompt += "\n\nReturn the emit_result tool call only, matching its schema exactly."
        response = await client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=max(256, int(os.environ.get("ANTHROPIC_MAX_OUTPUT_TOKENS", "8192"))),
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name": "emit_result",
                "description": "Emit the final structured result.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": "emit_result"},
            temperature=0,
        )
        _cost_tracker.accumulate_anthropic(response)
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            if getattr(block, "name", "") != "emit_result":
                continue
            try:
                return response_model.model_validate(block.input)
            except ValidationError as exc:
                last_error = str(exc)
                break
        else:
            last_error = f"stop_reason={getattr(response, 'stop_reason', None)}"

    if allow_none:
        return None
    raise RuntimeError(
        f"{component}: Anthropic native returned no valid structured output "
        f"after {max_attempts} attempt(s): {last_error}"
    )
