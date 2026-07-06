"""Helpers for model-scoped harness output directories."""

from __future__ import annotations

import os
import re
from pathlib import Path

import src.config  # noqa: F401 - side-effect: load .env into os.environ


def current_model_name() -> str:
    """Return the model name used by the active backend configuration."""
    backend = (
        os.environ.get("MODEL_BACKEND")
        or os.environ.get("LLM_BACKEND")
        or "anthropic"
    ).strip().lower()

    if backend in {"openai", "codex", "codex-pro"}:
        return (
            os.environ.get("OPENAI_MODEL")
            or os.environ.get("CODEX_PRO_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "unknown"
        )

    return os.environ.get("ANTHROPIC_MODEL") or "unknown"


def model_output_dir_name(model_name: str | None = None) -> str:
    """Return the per-model output directory name, e.g. outputs_gpt-5.2."""
    raw = (model_name or current_model_name() or "unknown").strip().lower()
    safe = re.sub(r"\s+", "-", raw)
    safe = re.sub(r"[^a-z0-9._-]+", "-", safe)
    safe = re.sub(r"-{2,}", "-", safe).strip("._-")
    return f"outputs_{safe or 'unknown'}"


def default_output_dir(parent: str | Path, model_name: str | None = None) -> Path:
    return Path(parent) / model_output_dir_name(model_name)
