"""Model-backend selection helpers.

The production default is intentionally unchanged: Claude Agent SDK. Set
`MODEL_BACKEND=openai` to route model calls through the native OpenAI backend.
"""

from __future__ import annotations

import os

import src.config  # noqa: F401 - side-effect: load .env into os.environ


def model_backend() -> str:
    raw = (
        os.environ.get("MODEL_BACKEND")
        or os.environ.get("LLM_BACKEND")
        or "anthropic"
    )
    backend = raw.strip().lower()
    if backend in {"claude", "anthropic"}:
        return "anthropic"
    if backend in {"openai", "codex", "codex-pro"}:
        return "openai"
    raise RuntimeError(
        f"Unsupported MODEL_BACKEND={raw!r}. Use 'anthropic' or 'openai'."
    )


def use_openai_backend() -> bool:
    return model_backend() == "openai"
