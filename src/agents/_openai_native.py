"""Native OpenAI Responses backend for the SWE-bench harness.

This module mirrors the small subset of Claude Agent SDK behavior the harness
depends on: structured JSON output plus a local tool loop for repository
inspection and patch application.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

from src.agents import _cost_tracker

T = TypeVar("T", bound=BaseModel)

ToolHandler = Callable[[dict[str, Any], Path | None], Awaitable[str]]


class OpenAIToolResult(BaseModel):
    result_text: str = ""
    subtype: str = "success"


def _client():
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "MODEL_BACKEND=openai requires the 'openai' package. "
            "Run: pip install -r requirements.txt"
        ) from exc

    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("CODEX_PRO_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "MODEL_BACKEND=openai requires OPENAI_API_KEY "
            "(or CODEX_PRO_API_KEY / ANTHROPIC_API_KEY fallback)."
        )
    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("BUZZ_BASE_URL")
        or os.environ.get("CODEX_PRO_BASE_URL")
        or None
    )
    if base_url and not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def _model() -> str:
    return (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("CODEX_PRO_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or "gpt-5"
    )


def _max_output_tokens() -> int:
    raw = os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "12000")
    try:
        return max(1024, int(raw))
    except ValueError:
        return 12000


def _reasoning() -> dict[str, Any] | None:
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip().lower()
    if not effort:
        return None
    return {"effort": effort}


def _api_surface() -> str:
    raw = os.environ.get("OPENAI_API_SURFACE", "chat_completions").strip().lower()
    if raw in {"responses", "response"}:
        return "responses"
    if raw in {"chat", "chat_completions", "chat.completions"}:
        return "chat_completions"
    raise RuntimeError(
        f"Unsupported OPENAI_API_SURFACE={raw!r}. "
        "Use 'responses' or 'chat_completions'."
    )


def _tool_choice(tools: list[dict[str, Any]]) -> str | None:
    return "auto" if tools else None


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # Do not mutate pydantic's cached schema dict.
    return json.loads(json.dumps(schema))


def _json_schema_format(response_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": response_model.__name__,
        "schema": _normalize_schema(response_model.model_json_schema()),
        # Keep false for pydantic schemas with defaulted fields. The caller
        # still validates with pydantic before accepting the result.
        "strict": False,
    }


def _chat_response_format(response_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_model.__name__,
            "schema": _normalize_schema(response_model.model_json_schema()),
            "strict": False,
        },
    }


def _content_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "\n".join(p for p in parts if p)
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)


def _message_text(response: object) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    pieces: list[str] = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "message":
            continue
        content = getattr(item, "content", None) or (
            item.get("content") if isinstance(item, dict) else []
        )
        for block in content or []:
            block_type = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if block_type in {"output_text", "text"}:
                text = getattr(block, "text", None) or (
                    block.get("text") if isinstance(block, dict) else ""
                )
                pieces.append(str(text))
    return "\n".join(pieces)


def _parse_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _resolve_schema_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/$defs/"):
        return schema
    name = ref.rsplit("/", 1)[-1]
    target = root.get("$defs", {}).get(name)
    return target if isinstance(target, dict) else schema


def _empty_for_schema(schema: dict[str, Any], root: dict[str, Any]) -> Any:
    schema = _resolve_schema_ref(schema, root)
    if "default" in schema:
        return json.loads(json.dumps(schema["default"]))
    typ = schema.get("type")
    if typ == "object" or "properties" in schema:
        return _fill_schema_defaults({}, schema, root)
    if typ == "array":
        return []
    if typ == "string":
        return ""
    if typ in {"integer", "number"}:
        return 0
    if typ == "boolean":
        return False
    if "anyOf" in schema:
        choices = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if choices:
            return _empty_for_schema(choices[0], root)
    return None


def _fill_schema_defaults(value: Any, schema: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
    root = root or schema
    schema = _resolve_schema_ref(schema, root)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key, prop_schema in props.items():
            if key not in value:
                value[key] = _empty_for_schema(prop_schema, root)
            else:
                value[key] = _fill_schema_defaults(value[key], prop_schema, root)
        return value
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [_fill_schema_defaults(item, item_schema, root) for item in value]
    return value


def _validate_with_defaults(response_model: type[T], payload: Any) -> T:
    schema = _normalize_schema(response_model.model_json_schema())
    filled = _fill_schema_defaults(payload, schema)
    return response_model.model_validate(filled)


def _function_calls(response: object) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "function_call":
            continue
        name = getattr(item, "name", None) or (
            item.get("name") if isinstance(item, dict) else None
        )
        call_id = getattr(item, "call_id", None) or (
            item.get("call_id") if isinstance(item, dict) else None
        )
        arguments = getattr(item, "arguments", None) or (
            item.get("arguments") if isinstance(item, dict) else "{}"
        )
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if name and call_id:
            calls.append({"name": name, "call_id": call_id, "arguments": args})
    return calls


def _response_output_items(response: object) -> list[dict[str, Any]]:
    """Return response output items suitable for stateless replay.

    Some OpenAI-compatible gateways expose `/v1/responses` but do not persist
    `previous_response_id`. Replaying the function_call items plus their
    function_call_output items keeps the tool loop portable.
    """
    out: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "function_call":
            continue
        if hasattr(item, "model_dump"):
            out.append(item.model_dump(exclude_none=True))
        elif isinstance(item, dict):
            out.append({k: v for k, v in item.items() if v is not None})
    return out


def _resolve_path(cwd: Path | None, path_text: str) -> Path:
    base = cwd or Path.cwd()
    raw = str(path_text or "").strip().replace("\\", "/")
    if not raw:
        return base
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _rel_path(cwd: Path | None, path: Path) -> str:
    if cwd is None:
        return str(path)
    try:
        return str(path.relative_to(cwd)).replace("\\", "/")
    except ValueError:
        return str(path)


def _is_skipped_dir(path: Path) -> bool:
    return any(
        part in {".git", ".venv", ".venv-ltm", "node_modules", "__pycache__"}
        for part in path.parts
    )


async def _tool_read(args: dict[str, Any], cwd: Path | None) -> str:
    file_arg = args.get("file_path") or args.get("filepath") or args.get("path")
    path = _resolve_path(cwd, str(file_arg or ""))
    if not path.is_file():
        return f"ERROR: file not found: {file_arg}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"ERROR: could not read {file_arg}: {exc}"
    try:
        offset = max(1, int(args.get("offset") or 1))
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(args.get("limit") or 240)
    except (TypeError, ValueError):
        limit = 240
    limit = max(1, min(limit, 1000))
    start = offset - 1
    selected = lines[start : start + limit]
    rel = _rel_path(cwd, path)
    numbered = [f"{start + idx + 1:>6}\t{line}" for idx, line in enumerate(selected)]
    return f"{rel}\n" + "\n".join(numbered)


async def _tool_glob(args: dict[str, Any], cwd: Path | None) -> str:
    pattern = str(args.get("pattern") or args.get("glob") or "").strip()
    if not pattern:
        return "ERROR: pattern is required."
    base = _resolve_path(cwd, str(args.get("path") or "."))
    if base.is_file():
        base = base.parent
    try:
        matches = [
            _rel_path(cwd, p)
            for p in base.glob(pattern)
            if not _is_skipped_dir(p)
        ]
    except OSError as exc:
        return f"ERROR: glob failed: {exc}"
    matches = sorted(matches)[:300]
    return "\n".join(matches) if matches else "No files found."


async def _tool_grep(args: dict[str, Any], cwd: Path | None) -> str:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "ERROR: pattern is required."
    root = _resolve_path(cwd, str(args.get("path") or "."))
    glob_pat = str(args.get("glob") or "**/*")
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))
    if root.is_file():
        files = [root]
    else:
        files = [
            p for p in root.rglob("*")
            if p.is_file()
            and not _is_skipped_dir(p)
            and fnmatch.fnmatch(_rel_path(root, p), glob_pat)
        ]
    out: list[str] = []
    for path in files[:5000]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                out.append(f"{_rel_path(cwd, path)}:{lineno}:{line[:500]}")
                if len(out) >= 200:
                    return "\n".join(out)
    return "\n".join(out) if out else "No matches found."


async def _tool_todo_write(args: dict[str, Any], cwd: Path | None) -> str:
    _ = cwd
    todos = args.get("todos", [])
    if isinstance(todos, list):
        return f"Recorded {len(todos)} todo item(s)."
    return "Recorded todo update."


async def _tool_apply_search_replace(args: dict[str, Any], cwd: Path | None) -> str:
    _ = cwd
    from src.tools.patch_tools import apply_search_replace

    result = await apply_search_replace(args)
    return _content_text(result)


async def _tool_search_ltm(args: dict[str, Any], cwd: Path | None) -> str:
    _ = cwd
    from src.tools.ltm_tools import search_ltm_experiences

    result = await search_ltm_experiences(args)
    return _content_text(result)


async def _tool_browse_ltm(args: dict[str, Any], cwd: Path | None) -> str:
    _ = cwd
    from src.tools.ltm_tools import browse_ltm_experience

    result = await browse_ltm_experience(args)
    return _content_text(result)


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "Read": {
        "description": "Read a file from the repository with line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        },
        "handler": _tool_read,
    },
    "Glob": {
        "description": "Find files by glob pattern under the repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
        "handler": _tool_glob,
    },
    "Grep": {
        "description": "Search repository files with a regex pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
        },
        "handler": _tool_grep,
    },
    "TodoWrite": {
        "description": "Record a todo-list update for the current agent step.",
        "parameters": {
            "type": "object",
            "properties": {"todos": {"type": "array", "items": {"type": "object"}}},
        },
        "handler": _tool_todo_write,
    },
    "mcp__patch__apply_search_replace": {
        "description": "Apply exact SEARCH/REPLACE edits to a target file.",
        "parameters": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string"},
                "blocks": {"type": "string"},
            },
            "required": ["filepath", "blocks"],
        },
        "handler": _tool_apply_search_replace,
    },
    "mcp__ltm__search_ltm_experiences": {
        "description": "Search long-term memory summary cards.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        "handler": _tool_search_ltm,
    },
    "mcp__ltm__browse_ltm_experience": {
        "description": "Browse one long-term-memory experience by id.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "handler": _tool_browse_ltm,
    },
}


def _openai_tool(name: str) -> dict[str, Any] | None:
    spec = _TOOL_SCHEMAS.get(name)
    if spec is None:
        return None
    return {
        "type": "function",
        "name": name,
        "description": spec["description"],
        "parameters": spec["parameters"],
    }


def _chat_tool(name: str) -> dict[str, Any] | None:
    spec = _TOOL_SCHEMAS.get(name)
    if spec is None:
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }


def _allowed_tool_specs(allowed_tools: list[str] | None) -> tuple[list[dict[str, Any]], dict[str, ToolHandler]]:
    tools: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}
    for name in allowed_tools or []:
        spec = _openai_tool(name)
        if spec is None:
            continue
        tools.append(spec)
        handlers[name] = _TOOL_SCHEMAS[name]["handler"]
    return tools, handlers


def _allowed_chat_tool_specs(allowed_tools: list[str] | None) -> tuple[list[dict[str, Any]], dict[str, ToolHandler]]:
    tools: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}
    for name in allowed_tools or []:
        spec = _chat_tool(name)
        if spec is None:
            continue
        tools.append(spec)
        handlers[name] = _TOOL_SCHEMAS[name]["handler"]
    return tools, handlers


async def _create_response(
    *,
    instructions: str,
    input_payload: Any,
    previous_response_id: str | None,
    tools: list[dict[str, Any]],
    response_model: type[BaseModel] | None,
) -> object:
    kwargs: dict[str, Any] = {
        "model": _model(),
        "input": input_payload,
        "tools": tools,
        "max_output_tokens": _max_output_tokens(),
    }
    if previous_response_id is None:
        kwargs["instructions"] = instructions
    else:
        kwargs["previous_response_id"] = previous_response_id
    tool_choice = _tool_choice(tools)
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    reasoning = _reasoning()
    if reasoning:
        kwargs["reasoning"] = reasoning
    if response_model is not None:
        kwargs["text"] = {"format": _json_schema_format(response_model)}
    return await _client().responses.create(**kwargs)


def _chat_message_text(message: object) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _chat_tool_calls(message: object) -> list[dict[str, Any]]:
    raw_calls = getattr(message, "tool_calls", None)
    if raw_calls is None and isinstance(message, dict):
        raw_calls = message.get("tool_calls")
    calls: list[dict[str, Any]] = []
    for call in raw_calls or []:
        call_id = getattr(call, "id", None) or (
            call.get("id") if isinstance(call, dict) else None
        )
        function = getattr(call, "function", None) or (
            call.get("function") if isinstance(call, dict) else None
        )
        name = getattr(function, "name", None) or (
            function.get("name") if isinstance(function, dict) else None
        )
        arguments = getattr(function, "arguments", None) or (
            function.get("arguments") if isinstance(function, dict) else "{}"
        )
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if call_id and name:
            calls.append({"id": call_id, "name": name, "arguments": args})
    return calls


async def _create_chat_completion(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    response_model: type[BaseModel] | None,
) -> object:
    kwargs: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if response_model is not None:
        kwargs["response_format"] = _chat_response_format(response_model)
    reasoning = _reasoning()
    if reasoning:
        # OpenAI reasoning chat models accept reasoning_effort; compatible
        # gateways that do not support it can leave OPENAI_REASONING_EFFORT unset.
        kwargs["reasoning_effort"] = reasoning["effort"]
    return await _client().chat.completions.create(**kwargs)


def _chat_usage_response(completion: object) -> object:
    class _Usage:
        input_tokens = 0
        output_tokens = 0

    class _Response:
        usage = _Usage()

    usage = getattr(completion, "usage", None)
    if usage is not None:
        _Response.usage.input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        _Response.usage.output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return _Response()


async def _run_chat_structured_query(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    component: str,
    allowed_tools: list[str] | None,
    max_turns: int,
    cwd: str | None,
    max_attempts: int,
    allow_none: bool,
) -> T | None:
    tools, handlers = _allowed_chat_tool_specs(allowed_tools)
    repo_cwd = Path(cwd).resolve() if cwd else None
    retry_nudge = (
        "\n\nIMPORTANT: Return only the JSON object matching the required schema."
    )
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        prompt = user_prompt if attempt == 1 else (user_prompt + retry_nudge)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        for _turn in range(max_turns):
            completion = await _create_chat_completion(
                messages=messages,
                tools=tools,
                response_model=response_model,
            )
            _cost_tracker.accumulate_openai(_chat_usage_response(completion))
            message = completion.choices[0].message
            calls = _chat_tool_calls(message)
            if not calls:
                text = _chat_message_text(message)
                try:
                    return _validate_with_defaults(response_model, _parse_json_object(text))
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    break

            messages.append(message.model_dump(exclude_none=True))
            for call in calls:
                handler = handlers.get(call["name"])
                if handler is None:
                    output = f"ERROR: tool {call['name']} is not available."
                else:
                    output = await handler(call["arguments"], repo_cwd)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": output,
                    }
                )
        else:
            last_error = "error_max_turns"

        if attempt < max_attempts:
            print(
                f"[{component}] OpenAI chat structured output failed "
                f"({last_error}); retrying (attempt {attempt + 1}/{max_attempts}).",
                flush=True,
            )

    if allow_none:
        print(
            f"[{component}] OpenAI chat returned no valid structured output after "
            f"{max_attempts} attempt(s): {last_error}; returning None.",
            flush=True,
        )
        return None
    raise RuntimeError(
        f"{component}: OpenAI chat returned no valid structured output after "
        f"{max_attempts} attempt(s): {last_error}"
    )


async def run_openai_structured_query(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    component: str,
    allowed_tools: list[str] | None = None,
    max_turns: int = 10,
    cwd: str | None = None,
    max_attempts: int = 2,
    allow_none: bool = False,
) -> T | None:
    """Run a structured Responses API call and pydantic-validate the result."""
    if _api_surface() == "chat_completions":
        return await _run_chat_structured_query(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            component=component,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            cwd=cwd,
            max_attempts=max_attempts,
            allow_none=allow_none,
        )

    tools, handlers = _allowed_tool_specs(allowed_tools)
    repo_cwd = Path(cwd).resolve() if cwd else None
    retry_nudge = (
        "\n\nIMPORTANT: Return only the JSON object matching the required schema."
    )
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        prompt = user_prompt if attempt == 1 else (user_prompt + retry_nudge)
        input_payload: Any = [{"role": "user", "content": prompt}]
        for _turn in range(max_turns):
            response = await _create_response(
                instructions=system_prompt,
                input_payload=input_payload,
                previous_response_id=None,
                tools=tools,
                response_model=response_model,
            )
            _cost_tracker.accumulate_openai(response)
            calls = _function_calls(response)
            if not calls:
                text = _message_text(response)
                try:
                    return _validate_with_defaults(response_model, _parse_json_object(text))
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    break
            outputs = []
            for call in calls:
                handler = handlers.get(call["name"])
                if handler is None:
                    output = f"ERROR: tool {call['name']} is not available."
                else:
                    output = await handler(call["arguments"], repo_cwd)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output,
                    }
                )
            input_payload = [
                *input_payload,
                *_response_output_items(response),
                *outputs,
            ]
        else:
            last_error = "error_max_turns"

        if attempt < max_attempts:
            print(
                f"[{component}] OpenAI structured output failed "
                f"({last_error}); retrying (attempt {attempt + 1}/{max_attempts}).",
                flush=True,
            )

    if allow_none:
        print(
            f"[{component}] OpenAI returned no valid structured output after "
            f"{max_attempts} attempt(s): {last_error}; returning None.",
            flush=True,
        )
        return None
    raise RuntimeError(
        f"{component}: OpenAI returned no valid structured output after "
        f"{max_attempts} attempt(s): {last_error}"
    )


async def run_openai_tool_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str | None,
    max_turns: int,
    response_model: type[T] | None = None,
) -> OpenAIToolResult | tuple[OpenAIToolResult, T]:
    """Run an OpenAI tool loop. Optionally parse final structured output."""
    if _api_surface() == "chat_completions":
        tools, handlers = _allowed_chat_tool_specs(allowed_tools)
        repo_cwd = Path(cwd).resolve() if cwd else None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_text = ""
        for _turn in range(max_turns):
            completion = await _create_chat_completion(
                messages=messages,
                tools=tools,
                response_model=response_model,
            )
            _cost_tracker.accumulate_openai(_chat_usage_response(completion))
            message = completion.choices[0].message
            calls = _chat_tool_calls(message)
            if not calls:
                last_text = _chat_message_text(message)
                result = OpenAIToolResult(result_text=last_text, subtype="success")
                if response_model is None:
                    return result
                parsed = _validate_with_defaults(response_model, _parse_json_object(last_text))
                return result, parsed

            messages.append(message.model_dump(exclude_none=True))
            for call in calls:
                handler = handlers.get(call["name"])
                if handler is None:
                    output = f"ERROR: tool {call['name']} is not available."
                else:
                    try:
                        output = await handler(call["arguments"], repo_cwd)
                    except Exception as exc:
                        output = f"ERROR: tool {call['name']} failed: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": output,
                    }
                )

        return OpenAIToolResult(result_text=last_text, subtype="error_max_turns")

    tools, handlers = _allowed_tool_specs(allowed_tools)
    repo_cwd = Path(cwd).resolve() if cwd else None
    input_payload: Any = [{"role": "user", "content": user_prompt}]
    last_response: object | None = None

    for _turn in range(max_turns):
        response = await _create_response(
            instructions=system_prompt,
            input_payload=input_payload,
            previous_response_id=None,
            tools=tools,
            response_model=response_model,
        )
        _cost_tracker.accumulate_openai(response)
        last_response = response
        calls = _function_calls(response)
        if not calls:
            text = _message_text(response)
            result = OpenAIToolResult(result_text=text, subtype="success")
            if response_model is None:
                return result
            parsed = _validate_with_defaults(response_model, _parse_json_object(text))
            return result, parsed

        outputs = []
        for call in calls:
            handler = handlers.get(call["name"])
            if handler is None:
                output = f"ERROR: tool {call['name']} is not available."
            else:
                try:
                    output = await handler(call["arguments"], repo_cwd)
                except Exception as exc:
                    output = f"ERROR: tool {call['name']} failed: {exc}"
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": output,
                }
            )
        input_payload = [
            *input_payload,
            *_response_output_items(response),
            *outputs,
        ]

    text = _message_text(last_response) if last_response is not None else ""
    return OpenAIToolResult(result_text=text, subtype="error_max_turns")


def run_openai_tool_agent_sync(**kwargs: Any) -> Any:
    return asyncio.run(run_openai_tool_agent(**kwargs))
