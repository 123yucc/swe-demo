"""Backend-neutral cost controls for model prompts and tool calls."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from src.agents.call_metrics import current as current_call_metrics

_tool_starts: dict[str, float] = {}
_tool_starts_lock = threading.Lock()


def int_setting(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except ValueError:
        return default


def tool_output_max_chars() -> int:
    """Return the shared output budget, honoring the old OpenAI name."""
    if "HARNESS_TOOL_OUTPUT_MAX_CHARS" in os.environ:
        return int_setting("HARNESS_TOOL_OUTPUT_MAX_CHARS", 16000)
    return int_setting("OPENAI_TOOL_OUTPUT_MAX_CHARS", 16000)


def anthropic_hooks() -> dict[str, list[Any]] | None:
    """Bound high-volume built-in Claude tools before they execute.

    Claude Agent SDK does not expose replacement of built-in tool responses.
    Limiting Read/Grep inputs is the lossless equivalent: the agent can page or
    narrow the next call instead of injecting an unbounded observation.
    """
    if os.environ.get("HARNESS_TOOL_INPUT_LIMITS", "on").strip().lower() in {
        "0", "false", "off", "no",
    }:
        return None

    from claude_agent_sdk import HookMatcher

    return {
        "PreToolUse": [HookMatcher(matcher="Read|Grep", hooks=[_pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher="Read|Grep", hooks=[_post_tool_use])],
        "PostToolUseFailure": [
            HookMatcher(matcher="Read|Grep", hooks=[_post_tool_failure])
        ],
    }


async def _pre_tool_use(
    hook_input: dict[str, Any], _tool_use_id: str | None, _context: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(hook_input.get("tool_name") or "")
    tool_input = dict(hook_input.get("tool_input") or {})
    use_id = str(hook_input.get("tool_use_id") or _tool_use_id or "")
    if use_id:
        with _tool_starts_lock:
            _tool_starts[use_id] = time.perf_counter()

    if tool_name == "Read":
        maximum = int_setting("HARNESS_READ_MAX_LINES", 240, minimum=1)
        try:
            requested = int(tool_input.get("limit") or maximum)
        except (TypeError, ValueError):
            requested = maximum
        tool_input["limit"] = min(maximum, max(1, requested))
    elif tool_name == "Grep":
        maximum = int_setting("HARNESS_GREP_MAX_RESULTS", 100, minimum=1)
        try:
            requested = int(tool_input.get("head_limit") or maximum)
        except (TypeError, ValueError):
            requested = maximum
        tool_input["head_limit"] = min(maximum, max(1, requested))

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": tool_input,
        }
    }


def _response_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _record_tool(hook_input: dict[str, Any], tool_use_id: str | None) -> None:
    use_id = str(hook_input.get("tool_use_id") or tool_use_id or "")
    started = None
    if use_id:
        with _tool_starts_lock:
            started = _tool_starts.pop(use_id, None)
    metrics = current_call_metrics()
    if metrics:
        chars = _response_chars(hook_input.get("tool_response", ""))
        metrics.tool(
            str(hook_input.get("tool_name") or "unknown"),
            max(0.0, time.perf_counter() - started) if started else 0.0,
            output_chars=chars,
            sent_chars=chars,
        )


async def _post_tool_use(
    hook_input: dict[str, Any], tool_use_id: str | None, _context: dict[str, Any],
) -> dict[str, Any]:
    _record_tool(hook_input, tool_use_id)
    return {}


async def _post_tool_failure(
    hook_input: dict[str, Any], tool_use_id: str | None, _context: dict[str, Any],
) -> dict[str, Any]:
    _record_tool(hook_input, tool_use_id)
    return {}
