from __future__ import annotations

import asyncio
import ssl

from pydantic import BaseModel

from src.agents import _openai_agents_sdk as agents_backend
from src.agents import _openai_native as native_backend
from src.models.verdict import ClosureVerdict


class Answer(BaseModel):
    value: str


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
