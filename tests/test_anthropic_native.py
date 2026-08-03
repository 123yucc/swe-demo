from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from src.agents import _anthropic_native, _cost_tracker


class Result(BaseModel):
    value: str


class _Messages:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="tool_use", name="emit_result", input={"value": "ok"}
            )],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=10,
                cache_creation_input_tokens=50,
                cache_read_input_tokens=25,
            ),
            stop_reason="tool_use",
        )


def test_native_structured_injects_ephemeral_cache_control(monkeypatch):
    messages = _Messages()
    monkeypatch.setattr(
        _anthropic_native,
        "AsyncAnthropic",
        lambda: SimpleNamespace(messages=messages),
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    _cost_tracker.reset()
    result = asyncio.run(_anthropic_native.run_anthropic_structured_query(
        system_prompt="stable system",
        user_prompt="case evidence",
        response_model=Result,
        component="test",
        max_attempts=1,
        allow_none=False,
    ))
    assert result == Result(value="ok")
    assert messages.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages.kwargs["messages"][0]["content"] == "case evidence"
    totals = _cost_tracker.get_totals()
    assert totals["cache_creation_input_tokens"] == 50
    assert totals["cache_read_input_tokens"] == 25
