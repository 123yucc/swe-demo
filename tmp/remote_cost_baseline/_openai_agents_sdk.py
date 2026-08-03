"""OpenAI Agents SDK implementation of the existing structured tool loop."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TypeVar

from agents import Agent, AgentOutputSchema, FunctionTool, ModelSettings, OpenAIProvider, RunConfig, Runner
from agents.lifecycle import RunHooksBase
from pydantic import BaseModel

from src.agents import _cost_tracker
from src.agents.call_metrics import current as current_call_metrics
from src.agents import _openai_native as native_backend
from src.agents._openai_native import _TOOL_SCHEMAS

T = TypeVar("T", bound=BaseModel)


def _provider() -> OpenAIProvider:
    api_key = native_backend._api_key()
    base_url = native_backend._base_url()
    http_client = native_backend._async_http_client()
    if http_client is None:
        return OpenAIProvider(api_key=api_key, base_url=base_url, use_responses=True)
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Agents SDK custom SSL configuration requires the 'openai' package."
        ) from exc
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=http_client,
    )
    return OpenAIProvider(openai_client=client, use_responses=True)


def _model() -> str:
    return os.environ.get("OPENAI_MODEL") or os.environ.get("CODEX_PRO_MODEL") or "gpt-5"


def _settings() -> ModelSettings:
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip().lower()
    reasoning = {"effort": effort} if effort else None
    return ModelSettings(
        max_tokens=max(1024, int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "12000"))),
        reasoning=reasoning,
        include_usage=True,
    )


def _tools(names: list[str] | None, cwd: str | None) -> list[FunctionTool]:
    repo_cwd = Path(cwd).resolve() if cwd else None
    tools: list[FunctionTool] = []
    for name in names or []:
        spec = _TOOL_SCHEMAS.get(name)
        if spec is None:
            continue

        async def invoke(_ctx, arguments: str, *, _name=name, _spec=spec):
            try:
                args = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            started = time.perf_counter()
            try:
                return await _spec["handler"](args, repo_cwd)
            finally:
                metrics = current_call_metrics()
                if metrics:
                    metrics.tool(_name, time.perf_counter() - started)

        tools.append(FunctionTool(
            name=name,
            description=spec["description"],
            params_json_schema=spec["parameters"],
            on_invoke_tool=invoke,
            strict_json_schema=False,
        ))
    return tools


class _UsageHooks(RunHooksBase):
    """Capture every model response, including invalid structured outputs."""

    async def on_llm_end(self, context, agent, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is not None:
            class UsageResponse:
                pass
            wrapped = UsageResponse()
            wrapped.usage = usage
            _cost_tracker.accumulate_openai(wrapped)
        metrics = current_call_metrics()
        if metrics:
            metrics.row["model_turns"] += 1


def _usage_hooks() -> _UsageHooks:
    return _UsageHooks()


def _accumulate_usage(result: object) -> None:
    """Backward-compatible helper for callers not using lifecycle hooks."""
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        class UsageResponse:
            pass
        wrapped = UsageResponse()
        wrapped.usage = usage
        _cost_tracker.accumulate_openai(wrapped)


def _error_status_code(exc: Exception) -> int | None:
    for attr in ("status_code",):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    status = _error_status_code(exc)
    if status == 429:
        return True
    text = str(exc).lower()
    return (
        "429" in text
        or "rate_limit" in text
        or "concurrency limit exceeded" in text
    )


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, min(30.0, float(retry_after)))
            except ValueError:
                pass
    return min(15.0, 3.0 * attempt)


async def run_agents_structured_query(
    *, system_prompt: str, user_prompt: str, response_model: type[T],
    component: str, allowed_tools: list[str] | None = None,
    max_turns: int = 10, cwd: str | None = None,
    max_attempts: int = 2, allow_none: bool = False,
) -> T | None:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        metrics = current_call_metrics()
        if metrics:
            metrics.retry(attempt)
        agent = Agent(
            name=component,
            instructions=system_prompt,
            model=_model(),
            model_settings=_settings(),
            tools=_tools(allowed_tools, cwd),
            output_type=AgentOutputSchema(response_model, strict_json_schema=False),
        )
        try:
            attempt_prompt = user_prompt
            if last_error is not None and not _is_rate_limit_error(last_error):
                # Replaying an identical prompt after schema validation fails
                # is not a corrective retry. Feed back only the concise tail
                # (full invalid JSON can be very large) so the next response
                # can repair the specific contract violation.
                error_tail = str(last_error)[-1800:]
                attempt_prompt += (
                    "\n\nPREVIOUS STRUCTURED OUTPUT WAS REJECTED. Correct the "
                    "output and return the complete schema again. Validation "
                    f"error:\n{error_tail}"
                )
            result = await Runner.run(
                agent,
                attempt_prompt,
                max_turns=max_turns,
                hooks=_usage_hooks(),
                run_config=RunConfig(
                    model_provider=_provider(),
                    tracing_disabled=True,
                    workflow_name=component,
                ),
            )
            if metrics:
                metrics.row["model_turns"] = max(metrics.row["model_turns"], 1)
            output = result.final_output
            return output if isinstance(output, response_model) else response_model.model_validate(output)
        except Exception as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            if _is_rate_limit_error(exc):
                delay = _retry_delay_seconds(exc, attempt)
                print(
                    f"[{component}] OpenAI Agents SDK rate-limited; "
                    f"backing off {delay:.1f}s before retry "
                    f"(attempt {attempt + 1}/{max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
    if allow_none:
        return None
    raise RuntimeError(
        f"{component}: Agents SDK returned no valid structured output after "
        f"{max_attempts} attempt(s): {last_error}"
    ) from last_error


async def run_agents_tool_agent(
    *, system_prompt: str, user_prompt: str,
    allowed_tools: list[str], cwd: str | None, max_turns: int,
):
    """Run the SDK agent loop for text-final-output components."""
    from src.agents._openai_native import OpenAIToolResult

    last_error: Exception | None = None
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        agent = Agent(
            name="openai-tool-agent",
            instructions=system_prompt,
            model=_model(),
            model_settings=_settings(),
            tools=_tools(allowed_tools, cwd),
        )
        try:
            result = await Runner.run(
                agent,
                user_prompt,
                max_turns=max_turns,
                hooks=_usage_hooks(),
                run_config=RunConfig(
                    model_provider=_provider(), tracing_disabled=True,
                    workflow_name="openai-tool-agent",
                ),
            )
            metrics = current_call_metrics()
            if metrics:
                metrics.row["model_turns"] = max(
                    metrics.row["model_turns"], len(getattr(result, "raw_responses", []) or [])
                )
            return OpenAIToolResult(result_text=str(result.final_output or ""), subtype="success")
        except Exception as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            if _is_rate_limit_error(exc):
                delay = _retry_delay_seconds(exc, attempt)
                print(
                    "[openai-tool-agent] OpenAI Agents SDK rate-limited; "
                    f"backing off {delay:.1f}s before retry "
                    f"(attempt {attempt + 1}/{max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
    raise RuntimeError(
        "openai-tool-agent: Agents SDK returned no valid result after "
        f"{max_attempts} attempt(s): {last_error}"
    ) from last_error
