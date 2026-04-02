"""Claude SDK adapter helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Literal, Optional, cast

from ..contracts.workflow import WorkerExecutionMode
from ..workers.registry import create_default_worker_specs


def _get_claude_executable_path() -> Optional[str]:
    """Resolve Claude CLI path from env or common install locations."""
    env_path = os.environ.get("CLAUDE_CODE_EXECUTABLE")
    if env_path and Path(env_path).exists():
        return env_path

    found = shutil.which("claude")
    if found:
        return found

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    common_paths = [
        Path(local_appdata) / "Programs" / "Claude" / "claude.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Claude" / "claude.exe",
    ]
    for candidate in common_paths:
        if candidate.exists():
            return str(candidate)

    return None


def get_project_cwd() -> str:
    """Return repository root used by Claude SDK calls."""
    return str(Path(__file__).resolve().parents[2])


def create_claude_options(phase: str = "full") -> Any:
    """Create Claude SDK options from WorkerSpec registry."""
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

    if phase == "phase1":
        allowed_tools = ["Read", "Glob", "Write"]
    elif phase == "phase2":
        allowed_tools = ["Read", "Grep", "Glob", "Bash"]
    else:
        allowed_tools = ["Task", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"]

    agents: Dict[str, AgentDefinition] = {}
    for worker_id, spec in create_default_worker_specs().items():
        if spec.execution_mode != WorkerExecutionMode.CLAUDE_AGENT:
            continue
        model = cast(Literal["sonnet", "opus", "haiku", "inherit"], spec.model)
        agents[worker_id] = AgentDefinition(
            description=spec.description,
            prompt=spec.prompt_template,
            tools=spec.allowed_tools,
            model=model,
        )

    return ClaudeAgentOptions(
        agents=agents,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        cli_path=_get_claude_executable_path(),
        cwd=get_project_cwd(),
        setting_sources=["project"],
    )


def create_evidence_extraction_options(*, cwd: str, agents: Dict[str, Any], output_schema: Optional[Dict[str, Any]] = None) -> Any:
    """Create Claude SDK options for phase1/phase2 evidence extraction."""
    from claude_agent_sdk import ClaudeAgentOptions

    options: Dict[str, Any] = {
        "cwd": cwd,
        "agents": agents,
        "allowed_tools": ["Read", "Grep", "Glob", "Agent"],
        "permission_mode": "bypassPermissions",
        "cli_path": _get_claude_executable_path(),
        "setting_sources": ["project"],
    }
    if output_schema is not None:
        options["output_format"] = {"type": "json_schema", "schema": output_schema}

    return ClaudeAgentOptions(
        **options,
    )


def _extract_first_json(text: str) -> Optional[Dict[str, Any]]:
    fenced = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            return None

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return None
    return None
