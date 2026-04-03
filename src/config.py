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


def sdk_env() -> dict[str, str]:
    """Return env overrides to inject into every ClaudeAgentOptions call."""
    env: dict[str, str] = {}
    if ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    if ANTHROPIC_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    return env
