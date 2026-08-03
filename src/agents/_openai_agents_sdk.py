"""OpenAI Agents SDK implementation of the existing structured tool loop."""

from __future__ import annotations

import asyncio
import hashlib
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
from src.agents import _model_infra
from src.agents._model_infra import ModelInfrastructureError
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


def _cache_key(
    system_prompt: str,
    allowed_tools: list[str] | None,
    response_model: type[BaseModel] | None,
    cwd: str | None,
) -> str | None:
    if not native_backend._prompt_cache_enabled():
        return None
    schemas = [
        {key: value for key, value in _TOOL_SCHEMAS[name].items() if key != "handler"}
        for name in allowed_tools or []
        if name in _TOOL_SCHEMAS
    ]
    key = native_backend._prompt_cache_key(system_prompt, schemas, response_model)
    try:
        shard_count = max(1, int(os.environ.get("OPENAI_PROMPT_CACHE_SHARDS", "4")))
    except ValueError:
        shard_count = 4
    if shard_count == 1:
        return key
    # Stable per-repository sharding avoids overloading one cache-routing key
    # when dozens of cases share the same component prefix.
    shard_source = str(Path(cwd).resolve()) if cwd else "global"
    shard = int(hashlib.sha256(shard_source.encode()).hexdigest()[:8], 16) % shard_count
    return f"{key}:s{shard}"


def _settings(
    *, system_prompt: str = "", allowed_tools: list[str] | None = None,
    response_model: type[BaseModel] | None = None, cwd: str | None = None,
    cache_key_enabled: bool = True,
) -> ModelSettings:
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip().lower()
    reasoning = {"effort": effort} if effort else None
    retention = native_backend._prompt_cache_retention()
    cache_key = (
        _cache_key(system_prompt, allowed_tools, response_model, cwd)
        if cache_key_enabled
        else None
    )
    return ModelSettings(
        max_tokens=max(1024, int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "12000"))),
        reasoning=reasoning,
        prompt_cache_retention=retention,
        include_usage=True,
        extra_args={"prompt_cache_key": cache_key} if cache_key else None,
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
            output_chars = 0
            sent_chars = 0
            try:
                output = await _spec["handler"](args, repo_cwd)
                output_chars = len(output)
                output = native_backend._bound_tool_output(_name, args, output)
                sent_chars = len(output)
                return output
            finally:
                metrics = current_call_metrics()
                if metrics:
                    metrics.tool(
                        _name,
                        time.perf_counter() - started,
                        output_chars=output_chars,
                        sent_chars=sent_chars,
                    )

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
    normalized = _model_infra.classify(exc)
    return normalized is not None and normalized.failure_kind == "api_rate_limit"


def _is_transient_connection_error(exc: Exception) -> bool:
    normalized = _model_infra.classify(exc)
    return normalized is not None and normalized.failure_kind in {
        "api_connection", "api_unavailable",
    }


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
    return min(30.0, 3.0 * 2 ** (attempt - 1))


def _rate_limit_max_attempts() -> int:
    raw = os.environ.get("OPENAI_RATE_LIMIT_MAX_ATTEMPTS", "3")
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(1, min(3, value))


async def run_agents_structured_query(
    *, system_prompt: str, user_prompt: str, response_model: type[T],
    component: str, allowed_tools: list[str] | None = None,
    max_turns: int = 10, cwd: str | None = None,
    max_attempts: int = 2, allow_none: bool = False,
) -> T | None:
    last_error: Exception | None = None
    cache_key_enabled = True
    logical_attempt = 1
    rate_limit_attempt = 0
    call_attempt = 0
    while logical_attempt <= max_attempts:
        call_attempt += 1
        metrics = current_call_metrics()
        if metrics:
            metrics.retry(call_attempt)
        agent = Agent(
            name=component,
            instructions=system_prompt,
            model=_model(),
            model_settings=_settings(
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                response_model=response_model,
                cwd=cwd,
                cache_key_enabled=cache_key_enabled,
            ),
            tools=_tools(allowed_tools, cwd),
            output_type=AgentOutputSchema(response_model, strict_json_schema=False),
        )
        try:
            _model_infra.before_request()
            attempt_prompt = user_prompt
            if (
                last_error is not None
                and not _is_rate_limit_error(last_error)
                and not _is_transient_connection_error(last_error)
            ):
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
            _model_infra.record_success()
            if metrics:
                metrics.row["model_turns"] = max(metrics.row["model_turns"], 1)
            output = result.final_output
            return output if isinstance(output, response_model) else response_model.model_validate(output)
        except Exception as exc:
            last_error = exc
            if cache_key_enabled and native_backend._cache_parameter_rejected(exc):
                cache_key_enabled = False
                print(
                    "[openai] gateway rejected prompt-cache parameters; "
                    "retrying without an explicit cache key.",
                    flush=True,
                )
                continue
            infra_error = _model_infra.classify(exc)
            if infra_error is not None:
                circuit_open = _model_infra.record_failure(infra_error)
                rate_limit_attempt += 1
                rate_limit_max_attempts = _rate_limit_max_attempts()
                if (
                    not infra_error.retryable
                    or circuit_open
                    or rate_limit_attempt >= rate_limit_max_attempts
                ):
                    break
                delay = _retry_delay_seconds(infra_error, rate_limit_attempt)
                print(
                    f"[{component}] OpenAI infrastructure failure "
                    f"({infra_error.failure_kind}); "
                    f"backing off {delay:.1f}s before retry "
                    f"(infra attempt {rate_limit_attempt + 1}/"
                    f"{rate_limit_max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
                continue
            if logical_attempt == max_attempts:
                break
            if _is_transient_connection_error(exc):
                delay = min(30.0, 5.0 * 2 ** (logical_attempt - 1))
                print(
                    f"[{component}] transient OpenAI connection failure; "
                    f"backing off {delay:.1f}s before retry "
                    f"(attempt {logical_attempt + 1}/{max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
            logical_attempt += 1
    if allow_none:
        return None
    infra_error = _model_infra.classify(last_error) if last_error else None
    if infra_error is not None:
        raise infra_error from last_error
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
    cache_key_enabled = True
    logical_attempt = 1
    rate_limit_attempt = 0
    while logical_attempt <= max_attempts:
        agent = Agent(
            name="openai-tool-agent",
            instructions=system_prompt,
            model=_model(),
            model_settings=_settings(
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                cwd=cwd,
                cache_key_enabled=cache_key_enabled,
            ),
            tools=_tools(allowed_tools, cwd),
        )
        try:
            _model_infra.before_request()
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
            _model_infra.record_success()
            metrics = current_call_metrics()
            if metrics:
                metrics.row["model_turns"] = max(
                    metrics.row["model_turns"], len(getattr(result, "raw_responses", []) or [])
                )
            return OpenAIToolResult(result_text=str(result.final_output or ""), subtype="success")
        except Exception as exc:
            last_error = exc
            if cache_key_enabled and native_backend._cache_parameter_rejected(exc):
                cache_key_enabled = False
                print(
                    "[openai] gateway rejected prompt-cache parameters; "
                    "retrying without an explicit cache key.",
                    flush=True,
                )
                continue
            infra_error = _model_infra.classify(exc)
            if infra_error is not None:
                circuit_open = _model_infra.record_failure(infra_error)
                rate_limit_attempt += 1
                rate_limit_max_attempts = _rate_limit_max_attempts()
                if (
                    not infra_error.retryable
                    or circuit_open
                    or rate_limit_attempt >= rate_limit_max_attempts
                ):
                    break
                delay = _retry_delay_seconds(infra_error, rate_limit_attempt)
                print(
                    "[openai-tool-agent] OpenAI infrastructure failure "
                    f"({infra_error.failure_kind}); "
                    f"backing off {delay:.1f}s before retry "
                    f"(infra attempt {rate_limit_attempt + 1}/"
                    f"{rate_limit_max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
                continue
            if logical_attempt == max_attempts:
                break
            if _is_transient_connection_error(exc):
                delay = min(30.0, 5.0 * 2 ** (logical_attempt - 1))
                print(
                    "[openai-tool-agent] transient OpenAI connection failure; "
                    f"backing off {delay:.1f}s before retry "
                    f"(attempt {logical_attempt + 1}/{max_attempts}).",
                    flush=True,
                )
                await asyncio.sleep(delay)
            logical_attempt += 1
    infra_error = _model_infra.classify(last_error) if last_error else None
    if infra_error is not None:
        raise infra_error from last_error
    raise RuntimeError(
        "openai-tool-agent: Agents SDK returned no valid result after "
        f"{max_attempts} attempt(s): {last_error}"
    ) from last_error
