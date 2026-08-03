"""Per-model-call JSONL instrumentation used by architecture experiments."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from src.agents import _cost_tracker

_path: Path | None = None
_lock = threading.Lock()
_current: ContextVar["CallRecorder | None"] = ContextVar("call_metrics", default=None)
_REQ_RE = re.compile(r"\breq-\d+\b")


def configure(path: str | Path | None) -> None:
    global _path
    _path = Path(path) if path else None
    if _path:
        _path.parent.mkdir(parents=True, exist_ok=True)


def model_label() -> str:
    return (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CODEX_PRO_MODEL")
        or "default"
    )


def write_event(row: dict[str, Any]) -> None:
    if not _path:
        return
    with _lock:
        with _path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class CallRecorder:
    def __init__(self, *, component: str, model: str, prompt: str,
                 call_reason: str = "", requirement_ids: list[str] | None = None):
        self.row: dict[str, Any] = {
            "component": component,
            "model": model,
            "requirement_ids": requirement_ids or sorted(set(_REQ_RE.findall(prompt))),
            "call_reason": call_reason or component,
            "prompt_chars": len(prompt),
            "attempt": 1,
            "model_turns": 0,
            "tool_calls": 0,
            "tool_durations_ms": {},
            "exception": None,
            "retry_count": 0,
        }
        self.started = 0.0
        self.before: dict[str, Any] = {}
        self.token = None

    def __enter__(self) -> "CallRecorder":
        self.started = time.perf_counter()
        self.before = _cost_tracker.get_totals()
        self.token = _current.set(self)
        return self

    def turn(self) -> None:
        self.row["model_turns"] += 1

    def retry(self, attempt: int) -> None:
        self.row["attempt"] = attempt
        self.row["retry_count"] = max(0, attempt - 1)

    def tool(self, name: str, elapsed_s: float) -> None:
        self.row["tool_calls"] += 1
        durations = self.row["tool_durations_ms"]
        durations[name] = round(durations.get(name, 0.0) + elapsed_s * 1000, 3)

    def __exit__(self, exc_type, exc, traceback) -> None:
        after = _cost_tracker.get_totals()
        self.row.update({
            "wall_clock_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "input_tokens": after["input_tokens"] - self.before["input_tokens"],
            "output_tokens": after["output_tokens"] - self.before["output_tokens"],
            "cache_creation_input_tokens": after["cache_creation_input_tokens"] - self.before["cache_creation_input_tokens"],
            "cache_read_input_tokens": after["cache_read_input_tokens"] - self.before["cache_read_input_tokens"],
        })
        if exc is not None:
            self.row["exception"] = f"{type(exc).__name__}: {exc}"
        if self.token is not None:
            _current.reset(self.token)
        write_event(self.row)


def current() -> CallRecorder | None:
    return _current.get()
