"""Shared model/API infrastructure error classification and circuit breaker."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path


class ModelInfrastructureError(RuntimeError):
    """A provider/relay failure that must not become a semantic agent failure."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = "api_unavailable",
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.status_code = status_code
        self.retryable = retryable


_LOCK = threading.Lock()
_FAILURES: deque[float] = deque()
_OPEN_UNTIL = 0.0


def _status_code(exc: BaseException) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int):
            return value
    return None


def classify(exc: BaseException) -> ModelInfrastructureError | None:
    """Return a normalized infrastructure error, or None for semantic errors."""
    if isinstance(exc, ModelInfrastructureError):
        return exc
    status = _status_code(exc)
    text = str(exc).lower()
    if status == 403 and any(
        marker in text
        for marker in ("insufficient_user_quota", "quota", "额度不足")
    ):
        return ModelInfrastructureError(
            str(exc), failure_kind="api_quota", status_code=403, retryable=False
        )
    if status == 429 or any(
        marker in text
        for marker in ("rate_limit", "concurrency limit exceeded", "too many requests")
    ):
        return ModelInfrastructureError(
            str(exc), failure_kind="api_rate_limit", status_code=429, retryable=True
        )
    if status in {500, 502, 503, 504} or any(
        marker in text
        for marker in (
            "service temporarily unavailable",
            "no available channel",
            "internal server error",
            "bad gateway",
            "gateway timeout",
        )
    ):
        return ModelInfrastructureError(
            str(exc),
            failure_kind="api_unavailable",
            status_code=status,
            retryable=True,
        )
    if any(
        marker in text
        for marker in (
            "connection error",
            "connection attempts failed",
            "connecterror",
            "connection reset",
            "timed out",
            "timeout",
            "certificate_verify_failed",
            "tls connection",
        )
    ):
        return ModelInfrastructureError(
            str(exc), failure_kind="api_connection", status_code=status, retryable=True
        )
    return None


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _state_path() -> Path | None:
    raw = os.environ.get("HARNESS_MODEL_CIRCUIT_PATH", "").strip()
    return Path(raw) if raw else None


def _write_state(status: str, **extra: object) -> None:
    path = _state_path()
    if path is None:
        return
    payload = {
        "schema_version": 1,
        "status": status,
        "updated_at_epoch": time.time(),
        **extra,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # Circuit enforcement must not depend on telemetry persistence.
        pass


def before_request() -> None:
    """Fail fast while the process-wide circuit is open."""
    global _OPEN_UNTIL
    now = time.monotonic()
    with _LOCK:
        if _OPEN_UNTIL > now:
            remaining = max(1, int(_OPEN_UNTIL - now))
            raise ModelInfrastructureError(
                f"model infrastructure circuit open for {remaining}s",
                failure_kind="api_circuit_open",
                retryable=True,
            )
        if _OPEN_UNTIL:
            _OPEN_UNTIL = 0.0
            _FAILURES.clear()
            _write_state("half_open")


def record_failure(error: ModelInfrastructureError) -> bool:
    """Record a failure and return True when it opens the circuit."""
    global _OPEN_UNTIL
    now = time.monotonic()
    threshold = _int_env("HARNESS_MODEL_CIRCUIT_THRESHOLD", 3)
    window = _int_env("HARNESS_MODEL_CIRCUIT_WINDOW_SECONDS", 60)
    cooldown = _int_env("HARNESS_MODEL_CIRCUIT_COOLDOWN_SECONDS", 300)
    with _LOCK:
        while _FAILURES and now - _FAILURES[0] > window:
            _FAILURES.popleft()
        _FAILURES.append(now)
        opened = len(_FAILURES) >= threshold
        if opened:
            _OPEN_UNTIL = max(_OPEN_UNTIL, now + cooldown)
        _write_state(
            "open" if opened else "closed",
            failure_count=len(_FAILURES),
            threshold=threshold,
            window_seconds=window,
            cooldown_seconds=cooldown,
            failure_kind=error.failure_kind,
            status_code=error.status_code,
            retryable=error.retryable,
            open_remaining_seconds=(
                max(0, int(_OPEN_UNTIL - now)) if opened else 0
            ),
        )
        return opened


def record_success() -> None:
    global _OPEN_UNTIL
    with _LOCK:
        _FAILURES.clear()
        _OPEN_UNTIL = 0.0
        _write_state("closed", failure_count=0)


def reset_for_tests() -> None:
    global _OPEN_UNTIL
    with _LOCK:
        _FAILURES.clear()
        _OPEN_UNTIL = 0.0
