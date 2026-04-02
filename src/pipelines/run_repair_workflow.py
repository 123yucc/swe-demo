"""Pipeline entrypoint for end-to-end repair workflow."""

from __future__ import annotations

from typing import Any, Dict

from ..orchestration import LLMOrchestrator


async def run_repair_workflow(workspace_dir: str, instance_id: str, prompt: str) -> Dict[str, Any]:
    """Run full workflow through LLM-first orchestrator."""
    orchestrator = LLMOrchestrator(workspace_dir, instance_id)
    result = await orchestrator.run(max_iterations=20)

    return {
        "instance_id": instance_id,
        "prompt": prompt,
        "success": result.success,
        "final_state": result.final_state,
        "completed_workers": result.completed_workers,
        "failed_workers": result.failed_workers,
        "iterations": result.iterations,
        "log_file": str((orchestrator.instance_dir / "logs" / "llm_orchestrator_events.jsonl")),
    }
