"""
Central place to configure API key and base URL for relay/proxy providers.

Reads from .env file in the project root (if present), then falls back to
environment variables already set in the shell.
"""

import os
from pathlib import Path

# Auto-load .env from project root (simple parser, no extra deps)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        os.environ.setdefault(_key.strip(), _val.strip())

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL: str = os.environ.get("ANTHROPIC_BASE_URL", "")
ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "MiniMax-M2.7")
ANTHROPIC_FALLBACK_MODEL: str = os.environ.get(
    "ANTHROPIC_FALLBACK_MODEL", "MiniMax-M2.7-highspeed"
)


def sdk_env() -> dict[str, str]:
    """Return env overrides to inject into every ClaudeAgentOptions call."""
    env: dict[str, str] = {}
    if ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    if ANTHROPIC_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    return env


def sdk_model_options() -> dict[str, str]:
    """Return model options for ClaudeAgentOptions.

    MiniMax Anthropic-compatible endpoints require MiniMax model names.
    Setting these explicitly avoids falling back to SDK default model names.
    """
    options: dict[str, str] = {}
    if ANTHROPIC_MODEL:
        options["model"] = ANTHROPIC_MODEL
    if ANTHROPIC_FALLBACK_MODEL:
        options["fallback_model"] = ANTHROPIC_FALLBACK_MODEL
    return options


def sdk_stderr_logger(component: str):
    """Create an SDK stderr callback with component prefix."""

    def _logger(line: str) -> None:
        msg = line.rstrip("\r\n")
        if msg:
            print(f"[sdk-stderr:{component}] {msg}", flush=True)

    return _logger
