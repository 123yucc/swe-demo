"""Load .env into os.environ so the Claude Agent SDK picks up ANTHROPIC_API_KEY.

Simple key=value parser, no extra deps. Anything the SDK needs (API key,
model selection, thinking/effort, etc.) is driven by environment variables
the SDK already understands — no wrapper helpers.
"""

import os
from pathlib import Path

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        os.environ[_key.strip()] = _val.strip()
