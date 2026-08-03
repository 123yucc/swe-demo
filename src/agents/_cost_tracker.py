"""Per-run cost and usage accumulator for the Claude Agent SDK.

All structured queries and patch-generator calls accumulate into this
module-level state. reset() is called at the start of each pipeline run
from main.py; get_totals() is called at the end to write run_metrics.json.
"""

from __future__ import annotations

import os

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
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        _usage["cache_read_input_tokens"] += int(cached or 0)
    if output_tokens is not None:
        _usage["output_tokens"] += int(output_tokens)


def accumulate_anthropic(response: object) -> None:
    """Accumulate native Anthropic Messages API usage."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
    ):
        value = getattr(usage, source, None)
        if value is not None:
            _usage[target] += int(value or 0)


def estimated_openai_cost_usd(
    *, input_tokens: int, cached_input_tokens: int, output_tokens: int,
) -> float:
    """Estimate OpenAI spend with model defaults and env-overridable rates."""
    model = (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("CODEX_PRO_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or ""
    ).lower()
    defaults = (1.75, 0.175, 14.0) if model.startswith("gpt-5.2") else (0.0, 0.0, 0.0)

    def rate(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default

    input_rate = rate("OPENAI_INPUT_USD_PER_MTOK", defaults[0])
    cached_rate = rate("OPENAI_CACHED_INPUT_USD_PER_MTOK", defaults[1])
    output_rate = rate("OPENAI_OUTPUT_USD_PER_MTOK", defaults[2])
    cached = max(0, min(input_tokens, cached_input_tokens))
    uncached = max(0, input_tokens - cached)
    return round(
        (uncached * input_rate + cached * cached_rate + output_tokens * output_rate)
        / 1_000_000,
        6,
    )


def estimated_anthropic_cost_usd(
    *, input_tokens: int, cache_creation_tokens: int,
    cache_read_tokens: int, output_tokens: int,
) -> float:
    """Estimate Claude cost; every rate remains environment-overridable."""
    def rate(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, str(default))))
        except ValueError:
            return default

    # Anthropic reports uncached input separately from cache write/read tokens.
    regular = max(0, input_tokens)
    return round((
        regular * rate("ANTHROPIC_INPUT_USD_PER_MTOK", 3.0)
        + cache_creation_tokens * rate("ANTHROPIC_CACHE_WRITE_USD_PER_MTOK", 3.75)
        + cache_read_tokens * rate("ANTHROPIC_CACHE_READ_USD_PER_MTOK", 0.30)
        + output_tokens * rate("ANTHROPIC_OUTPUT_USD_PER_MTOK", 15.0)
    ) / 1_000_000, 6)


def get_totals() -> dict:
    backend = os.environ.get("MODEL_BACKEND", "anthropic").strip().lower()
    if backend in {"anthropic", "claude"}:
        estimated = estimated_anthropic_cost_usd(
            input_tokens=_usage["input_tokens"],
            cache_creation_tokens=_usage["cache_creation_input_tokens"],
            cache_read_tokens=_usage["cache_read_input_tokens"],
            output_tokens=_usage["output_tokens"],
        )
    else:
        estimated = estimated_openai_cost_usd(
            input_tokens=_usage["input_tokens"],
            cached_input_tokens=_usage["cache_read_input_tokens"],
            output_tokens=_usage["output_tokens"],
        )
    return {
        "total_cost_usd": _total_cost_usd,
        "estimated_cost_usd": estimated,
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
