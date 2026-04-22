"""
Relay-compatible structured-output helper.

Anthropic's native `output_format={"type": "json_schema", ...}` relies on
constrained decoding inside the Anthropic inference stack.  Third-party
Anthropic-compatible relays (e.g. MiniMax) do not implement constrained
decoding; the schema becomes a soft hint, the model freely emits JSON, and
pydantic validation fails on enum typos / missing fields / wrong nesting.
The SDK then burns 3-5 silent retries (same prompt, same outcome) before
raising, consuming tool turns and budget for nothing.

This helper bypasses `output_format` entirely: it asks the model to emit a
fenced JSON block, extracts + validates against a pydantic model, and on
failure re-prompts with the ValidationError text so the model can
self-correct.  No SDK-level retries are involved.
"""

from __future__ import annotations

import json
from typing import TypeVar

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel, ValidationError

from src.config import sdk_env, sdk_model_options, sdk_stderr_logger

T = TypeVar("T", bound=BaseModel)


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object from free-form model output.

    Handles:
      - fenced ```json ... ``` blocks
      - fenced ``` ... ``` blocks without a language tag
      - raw JSON with no fence
      - JSON embedded inside prose (takes first-{ to last-})
    """
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            body = "\n".join(lines[1:-1]).strip()
            if body.lower().startswith("json"):
                body = body[4:].lstrip()
            raw = body

    candidates: list[str] = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _build_schema_hint(model: type[BaseModel]) -> str:
    """Compact JSON-Schema rendering for inclusion in the prompt."""
    return json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False)


async def run_structured_query(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    component: str,
    allowed_tools: list[str] | None = None,
    max_turns: int = 10,
    max_budget_usd: float = 1.0,
    max_validation_retries: int = 3,
    permission_mode: str = "acceptEdits",
    cwd: str | None = None,
) -> T:
    """Run a query that must return an instance of *response_model*.

    On validation failure, re-prompts with the error so the model self-corrects.
    Does NOT use the SDK's built-in `output_format` (incompatible with MiniMax
    relay — see docs/api.md).

    Args:
        system_prompt:  System message for the agent.
        user_prompt:    First-turn user message.
        response_model: Pydantic model the final JSON must validate against.
        component:      Short name used in log prefixes ("parser", ...).
        allowed_tools:  SDK tool allowlist (e.g. ["Grep", "Read", "Glob"]).
        max_turns:      Per-query SDK tool-turn cap.
        max_budget_usd: Per-query SDK USD cap.
        max_validation_retries: How many re-prompt rounds after the first.
        permission_mode: SDK permission mode (default "acceptEdits").

    Returns:
        A validated *response_model* instance.
    """
    allowed_tools = allowed_tools or []
    schema_hint = _build_schema_hint(response_model)
    base_user_prompt = (
        f"{user_prompt.rstrip()}\n\n"
        "Return exactly ONE JSON object matching the schema below, inside a "
        "single fenced block delimited by ```json and ```. Emit no prose, "
        "commentary, or extra fences before or after the JSON block.\n\n"
        f"Schema:\n```json\n{schema_hint}\n```"
    )

    options_kwargs: dict = dict(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        max_thinking_tokens=0,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        **sdk_model_options(),
        stderr=sdk_stderr_logger(component),
        env=sdk_env(),
    )
    if cwd is not None:
        options_kwargs["cwd"] = cwd
    options = ClaudeAgentOptions(**options_kwargs)

    prompt = base_user_prompt
    last_error: str = ""
    last_text: str = ""

    total_attempts = max_validation_retries + 1
    for attempt in range(1, total_attempts + 1):
        result_message: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result_message = message

        if result_message is None:
            raise RuntimeError(f"{component}: SDK returned no ResultMessage.")

        if result_message.subtype in ("error_max_turns", "error_max_budget_usd"):
            raise RuntimeError(
                f"{component}: aborted due to per-query limit "
                f"({result_message.subtype})."
            )

        last_text = result_message.result or ""
        candidate = _extract_json_object(last_text)

        if candidate is None:
            last_error = "Response contained no parseable JSON object."
        else:
            try:
                return response_model.model_validate(candidate)
            except ValidationError as exc:
                last_error = str(exc)

        if attempt < total_attempts:
            print(
                f"[{component}] structured-output validation failed "
                f"(attempt {attempt}/{total_attempts}); re-prompting with error",
                flush=True,
            )
            truncated_prev = last_text[:2000]
            prompt = (
                f"{base_user_prompt}\n\n"
                "Your previous reply failed validation. Correct it and reply "
                "again with only the JSON block.\n\n"
                f"Previous reply (truncated):\n```\n{truncated_prev}\n```\n\n"
                f"Validation error:\n```\n{last_error}\n```"
            )

    raise RuntimeError(
        f"{component}: structured output failed after {total_attempts} attempts. "
        f"Last error: {last_error}"
    )
