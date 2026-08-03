from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src.agents import _model_infra
from src.agents import _openai_agents_sdk as agents_backend
from src.agents import _openai_native as native_backend
from src.agents import _cost_tracker
from src.agents import _structured
from src.models.verdict import ClosureVerdict


class Answer(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def _reset_model_infra_circuit():
    _model_infra.reset_for_tests()
    yield
    _model_infra.reset_for_tests()


def test_agents_sdk_tool_adapter_preserves_schema_and_handler(monkeypatch, tmp_path):
    called = {}

    async def handler(args, cwd):
        called.update(args)
        called["cwd"] = cwd
        return "ok"

    monkeypatch.setitem(agents_backend._TOOL_SCHEMAS, "Probe", {
        "description": "probe",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        "handler": handler,
    })
    tool = agents_backend._tools(["Probe"], str(tmp_path))[0]
    assert tool.params_json_schema["required"] == ["x"]
    result = asyncio.run(tool.on_invoke_tool(None, '{"x":"yes"}'))
    assert result == "ok"
    assert called["x"] == "yes"
    assert called["cwd"] == tmp_path.resolve()


def test_agents_sdk_tool_adapter_bounds_output(monkeypatch):
    async def handler(args, cwd):
        return "\n".join(f"match-{i}-" + "x" * 30 for i in range(50))

    monkeypatch.setenv("OPENAI_TOOL_OUTPUT_MAX_CHARS", "250")
    monkeypatch.setitem(agents_backend._TOOL_SCHEMAS, "GrepProbe", {
        "description": "probe",
        "parameters": {"type": "object", "properties": {}},
        "handler": handler,
    })
    tool = agents_backend._tools(["GrepProbe"], None)[0]

    result = asyncio.run(tool.on_invoke_tool(None, "{}"))

    assert len(result) <= 250
    assert "tool output clipped" in result


def test_agents_sdk_settings_enable_supported_cache_retention(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "auto")
    assert agents_backend._settings().prompt_cache_retention == "24h"


def test_agents_sdk_settings_use_stable_sharded_cache_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE", "auto")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_SHARDS", "4")

    first = agents_backend._settings(
        system_prompt="stable",
        allowed_tools=["Read"],
        response_model=Answer,
        cwd=str(tmp_path),
    )
    second = agents_backend._settings(
        system_prompt="stable",
        allowed_tools=["Read"],
        response_model=Answer,
        cwd=str(tmp_path),
    )

    assert first.extra_args == second.extra_args
    assert first.extra_args["prompt_cache_key"].startswith("swe-agent:")
    assert first.extra_args["prompt_cache_key"][-3:-1] == ":s"


def test_agents_sdk_settings_can_disable_explicit_cache_key(monkeypatch):
    monkeypatch.setenv("OPENAI_PROMPT_CACHE", "auto")
    settings = agents_backend._settings(
        system_prompt="stable", cache_key_enabled=False
    )
    assert settings.extra_args is None


def test_agents_sdk_recognizes_transient_connection_failures():
    assert agents_backend._is_transient_connection_error(Exception("Connection error."))
    assert agents_backend._is_transient_connection_error(
        Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )
    assert not agents_backend._is_transient_connection_error(Exception("schema mismatch"))


def test_agents_sdk_does_not_misclassify_401_request_id_as_rate_limit():
    class AuthenticationError(Exception):
        status_code = 401

    assert not agents_backend._is_rate_limit_error(
        AuthenticationError("Invalid token request id: 202607180429123")
    )


def test_agents_sdk_provider_uses_responses_and_normalizes_v1(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/root")
    monkeypatch.delenv("OPENAI_CA_CERT_PATH", raising=False)
    monkeypatch.delenv("OPENAI_SSL_VERIFY", raising=False)
    provider = agents_backend._provider()
    assert provider._stored_base_url.rstrip("/").endswith("/root/v1")
    assert provider._use_responses is True


def test_openai_ssl_verify_setting_prefers_ca_cert_path(monkeypatch, tmp_path):
    cert = tmp_path / "ca.pem"
    cert.write_text("cert", encoding="utf-8")
    monkeypatch.setenv("OPENAI_CA_CERT_PATH", str(cert))
    monkeypatch.delenv("OPENAI_SSL_VERIFY", raising=False)
    sentinel = object()
    monkeypatch.setattr(native_backend, "_ca_bundle_context", lambda path: sentinel)
    verify = native_backend._ssl_verify_setting()
    assert verify is sentinel


def test_openai_ssl_verify_setting_accepts_false(monkeypatch):
    monkeypatch.delenv("OPENAI_CA_CERT_PATH", raising=False)
    monkeypatch.setenv("OPENAI_SSL_VERIFY", "false")
    assert native_backend._ssl_verify_setting() is False


def test_read_tool_output_is_bounded_with_continuation_offset(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_OUTPUT_MAX_CHARS", "300")
    output = "src/large.py\n" + "\n".join(
        f"{line:>6}\t" + ("x" * 40) for line in range(20, 80)
    )

    bounded = native_backend._bound_tool_output("Read", {"offset": 20}, output)

    assert len(bounded) <= 300
    assert bounded.startswith("src/large.py\n")
    assert "tool output clipped" in bounded
    assert "continue with Read offset=" in bounded


def test_search_tool_output_keeps_head_and_tail(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_OUTPUT_MAX_CHARS", "260")
    output = "\n".join(f"src/f.py:{i}:match-{i}-" + "x" * 20 for i in range(40))

    bounded = native_backend._bound_tool_output("Grep", {}, output)

    assert len(bounded) <= 260
    assert "match-0" in bounded
    assert "match-39" in bounded
    assert "tool output clipped" in bounded


def test_patch_tool_output_is_never_clipped(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_OUTPUT_MAX_CHARS", "100")
    output = "applied\n" + "x" * 1000
    assert native_backend._bound_tool_output(
        "mcp__patch__apply_search_replace", {}, output
    ) == output


def test_prompt_cache_kwargs_are_stable_for_same_contract(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE", "auto")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "auto")
    tools = [{"type": "function", "name": "Read"}]

    first = native_backend._cache_request_kwargs("stable", tools, Answer)
    second = native_backend._cache_request_kwargs("stable", tools, Answer)

    assert first == second
    assert first["prompt_cache_key"].startswith("swe-agent:")
    assert first["prompt_cache_retention"] == "24h"


def test_chat_usage_tracks_cached_input_tokens():
    completion = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64),
    ))

    response = native_backend._chat_usage_response(completion)

    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.input_tokens_details.cached_tokens == 64


def test_gpt52_cost_estimate_uses_cached_input_rate(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")
    cost = _cost_tracker.estimated_openai_cost_usd(
        input_tokens=1_000_000,
        cached_input_tokens=600_000,
        output_tokens=100_000,
    )
    assert cost == 2.205


def test_agents_sdk_provider_uses_openai_client_when_custom_ssl_is_configured(monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/root")
    monkeypatch.setattr(agents_backend, "OpenAIProvider", FakeProvider)
    monkeypatch.setattr(native_backend, "_async_http_client", lambda: object())

    import sys
    import types

    fake_mod = types.SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_mod)

    provider = agents_backend._provider()

    assert isinstance(provider, FakeProvider)
    assert "openai_client" in captured
    assert isinstance(captured["openai_client"], FakeAsyncOpenAI)
    assert captured["openai_client"].kwargs["base_url"].rstrip("/").endswith("/root/v1")
    assert captured["use_responses"] is True


def test_agents_sdk_accepts_non_strict_closure_schema(monkeypatch):
    captured = {}
    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr(agents_backend, "Agent", FakeAgent)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    class FakeRunner:
        @staticmethod
        async def run(*args, **kwargs):
            raise RuntimeError("stop after construction")
    monkeypatch.setattr(agents_backend, "Runner", FakeRunner)
    try:
        asyncio.run(agents_backend.run_agents_structured_query(
            system_prompt="x", user_prompt="y", response_model=ClosureVerdict,
            component="closure", max_attempts=1,
        ))
    except RuntimeError:
        pass
    assert captured["output_type"].is_strict_json_schema() is False


def test_agents_sdk_retry_includes_validation_feedback(monkeypatch):
    prompts = []

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

    class FakeResult:
        final_output = Answer(value="fixed")
        raw_responses = []

    class FakeRunner:
        @staticmethod
        async def run(agent, prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                raise ValueError("field x must name only one requirement")
            return FakeResult()

    monkeypatch.setattr(agents_backend, "Agent", FakeAgent)
    monkeypatch.setattr(agents_backend, "Runner", FakeRunner)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())

    result = asyncio.run(agents_backend.run_agents_structured_query(
        system_prompt="system", user_prompt="original",
        response_model=Answer, component="test", max_attempts=2,
    ))

    assert result == Answer(value="fixed")
    assert prompts[0] == "original"
    assert "PREVIOUS STRUCTURED OUTPUT WAS REJECTED" in prompts[1]
    assert "must name only one requirement" in prompts[1]


def test_agents_sdk_rate_limit_retries_with_backoff_without_prompt_pollution(monkeypatch):
    prompts = []
    sleeps = []

    class RateLimitError(RuntimeError):
        status_code = 429

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

    class FakeResult:
        final_output = Answer(value="fixed")
        raw_responses = []

    class FakeRunner:
        @staticmethod
        async def run(agent, prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                raise RateLimitError("Concurrency limit exceeded for user")
            return FakeResult()

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(agents_backend, "Agent", FakeAgent)
    monkeypatch.setattr(agents_backend, "Runner", FakeRunner)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    monkeypatch.setattr(agents_backend.asyncio, "sleep", fake_sleep)

    result = asyncio.run(agents_backend.run_agents_structured_query(
        system_prompt="system", user_prompt="original",
        response_model=Answer, component="test", max_attempts=2,
    ))

    assert result == Answer(value="fixed")
    assert prompts == ["original", "original"]
    assert sleeps == [3.0]


def test_agents_sdk_rate_limit_is_bounded_and_opens_circuit(monkeypatch):
    prompts = []
    sleeps = []

    class RateLimitError(RuntimeError):
        status_code = 429

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

    class FakeResult:
        final_output = Answer(value="fixed")
        raw_responses = []

    class FakeRunner:
        @staticmethod
        async def run(agent, prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) <= 3:
                raise RateLimitError("Concurrency limit exceeded for user")
            return FakeResult()

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(agents_backend, "Agent", FakeAgent)
    monkeypatch.setattr(agents_backend, "Runner", FakeRunner)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    monkeypatch.setattr(agents_backend.asyncio, "sleep", fake_sleep)

    with pytest.raises(_model_infra.ModelInfrastructureError):
        asyncio.run(agents_backend.run_agents_structured_query(
            system_prompt="system", user_prompt="original",
            response_model=Answer, component="test", max_attempts=2,
        ))

    assert prompts == ["original"] * 3
    assert sleeps == [3.0, 6.0]


def test_agents_sdk_rate_limit_retry_budget_defaults_to_hard_cap(monkeypatch):
    monkeypatch.delenv("OPENAI_RATE_LIMIT_MAX_ATTEMPTS", raising=False)
    assert agents_backend._rate_limit_max_attempts() == 3

    monkeypatch.setenv("OPENAI_RATE_LIMIT_MAX_ATTEMPTS", "invalid")
    assert agents_backend._rate_limit_max_attempts() == 3

    monkeypatch.setenv("OPENAI_RATE_LIMIT_MAX_ATTEMPTS", "10")
    assert agents_backend._rate_limit_max_attempts() == 3


def test_agents_sdk_503_is_model_infrastructure_failure(monkeypatch):
    class Unavailable(RuntimeError):
        status_code = 503

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

    async def fail(*args, **kwargs):
        raise Unavailable("Service temporarily unavailable")

    async def no_sleep(delay):
        pass

    monkeypatch.setattr(agents_backend, "Agent", FakeAgent)
    monkeypatch.setattr(agents_backend.Runner, "run", fail)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    monkeypatch.setattr(agents_backend.asyncio, "sleep", no_sleep)

    with pytest.raises(_model_infra.ModelInfrastructureError) as caught:
        asyncio.run(agents_backend.run_agents_structured_query(
            system_prompt="system", user_prompt="original",
            response_model=Answer, component="test", max_attempts=2,
        ))
    assert caught.value.failure_kind == "api_unavailable"


def test_text_tool_agent_returns_native_compatible_result(monkeypatch):
    class FakeResult:
        final_output = "done"
        raw_responses = []
    async def fake_run(*args, **kwargs):
        return FakeResult()
    monkeypatch.setattr(agents_backend.Runner, "run", fake_run)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    result = asyncio.run(agents_backend.run_agents_tool_agent(
        system_prompt="x", user_prompt="y", allowed_tools=[], cwd=None, max_turns=2,
    ))
    assert result.result_text == "done"
    assert result.subtype == "success"


def test_text_tool_agent_retries_rate_limit(monkeypatch):
    sleeps = []
    calls = []

    class RateLimitError(RuntimeError):
        status_code = 429

    class FakeResult:
        final_output = "done"
        raw_responses = []

    async def fake_run(*args, **kwargs):
        calls.append("run")
        if len(calls) == 1:
            raise RateLimitError("rate_limit_error")
        return FakeResult()

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(agents_backend.Runner, "run", fake_run)
    monkeypatch.setattr(agents_backend, "_provider", lambda: object())
    monkeypatch.setattr(agents_backend.asyncio, "sleep", fake_sleep)

    result = asyncio.run(agents_backend.run_agents_tool_agent(
        system_prompt="x", user_prompt="y", allowed_tools=[], cwd=None, max_turns=2,
    ))
    assert result.result_text == "done"
    assert result.subtype == "success"
    assert sleeps == [3.0]


def test_close_structured_clients_ignores_sdk_cancelled_disconnect():
    class Client:
        async def disconnect(self):
            raise asyncio.CancelledError()

    _structured._CLAUDE_CLIENTS[("test",)] = Client()
    asyncio.run(_structured.close_structured_clients())
    assert not _structured._CLAUDE_CLIENTS
