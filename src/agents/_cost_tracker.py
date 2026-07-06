"""Per-run cost and usage accumulator for the Claude Agent SDK.

All structured queries and patch-generator calls accumulate into this
module-level state. reset() is called at the start of each pipeline run
from main.py; get_totals() is called at the end to write run_metrics.json.
"""

from __future__ import annotations

_total_cost_usd: float = 0.0
_usage: dict[str, int] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def accumulate(result_message: object) -> None:
    """Accumulate cost/usage from a ResultMessage."""
    global _total_cost_usd, _usage
    cost = getattr(result_message, "total_cost_usd", None)
    if cost is not None:
        _total_cost_usd += float(cost)
    usage = getattr(result_message, "usage", None)
    if usage and isinstance(usage, dict):
        for key in _usage:
            _usage[key] += int(usage.get(key, 0))


def accumulate_openai(response: object) -> None:
    """Accumulate token usage from an OpenAI Responses API response.

    OpenAI responses do not include USD cost in the API object, so cost remains
    zero for native OpenAI runs. Token counters are still useful for comparing
    backend behavior.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is not None:
        _usage["input_tokens"] += int(input_tokens)
    if output_tokens is not None:
        _usage["output_tokens"] += int(output_tokens)


def get_totals() -> dict:
    return {
        "total_cost_usd": _total_cost_usd,
        "input_tokens": _usage["input_tokens"],
        "output_tokens": _usage["output_tokens"],
        "cache_creation_input_tokens": _usage["cache_creation_input_tokens"],
        "cache_read_input_tokens": _usage["cache_read_input_tokens"],
    }


def reset() -> None:
    global _total_cost_usd, _usage
    _total_cost_usd = 0.0
    _usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
