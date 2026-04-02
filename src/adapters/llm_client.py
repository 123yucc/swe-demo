"""Claude SDK adapter helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Literal, Optional, cast

from ..contracts.worker_protocol import OrchestrationDecision
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


async def propose_orchestration_decision(context: Dict[str, Any]) -> OrchestrationDecision:
    """Ask LLM orchestrator for one-step decision; fallback to heuristic when unavailable."""
    loop_state = str(context.get("loop_state", "under_specified"))
    ready_workers = [str(worker) for worker in context.get("ready_workers", [])]
    completed_workers = set(str(worker) for worker in context.get("completed_workers", []))

    try:
        if os.environ.get("ORCHESTRATOR_USE_LLM", "0") == "1":
            from claude_agent_sdk import query

            prompt = (
                "Return only JSON for one-step orchestration decision with keys: "
                "next_phase, selected_workers, todo_actions, state_transition_reason, confidence.\n"
                f"Context: {json.dumps(context, ensure_ascii=False)}"
            )
            options = create_claude_options(phase="full")

            latest = ""
            async for message in query(prompt=prompt, options=options):
                latest = str(getattr(message, "result", message))

            parsed = _extract_first_json(latest)
            if parsed is not None:
                return OrchestrationDecision.model_validate(parsed)
    except Exception:
        pass

    # Heuristic fallback
    next_phase = loop_state
    if loop_state == "init":
        next_phase = "under_specified"
    elif loop_state == "under_specified":
        next_phase = "evidence_refining"
    elif loop_state == "evidence_refining" and "closure-checker" in ready_workers:
        next_phase = "closed"
    elif loop_state == "closed":
        if "validator" in completed_workers:
            next_phase = "patch_success"
        else:
            next_phase = "closed"

    return OrchestrationDecision(
        next_phase=next_phase,
        selected_workers=ready_workers[:1],
        todo_actions=[],
        state_transition_reason="fallback_decision",
        confidence=0.2,
    )
