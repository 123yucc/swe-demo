"""Deterministic worker runtime for execution/retry/recording."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any, Dict, Optional, Tuple

from ..contracts.worker_protocol import WorkerExecutionResult
from ..contracts.workflow import WorkerSpec


_EXECUTOR_MAP: Dict[str, Tuple[str, str]] = {
    "artifact-parser": ("src.workers.phase1_artifact_parser", "run_phase1_parsing"),
    "symptom-extractor": ("src.workers.phase2_symptom_extractor", "extract_symptom_evidence"),
    "localization-extractor": ("src.workers.phase2_localization_extractor", "extract_localization_evidence"),
    "constraint-extractor": ("src.workers.phase2_constraint_extractor", "extract_constraint_evidence"),
    "structural-extractor": ("src.workers.phase2_structural_extractor", "extract_structural_evidence"),
    "llm-enhancer": ("src.workers.phase2_enhancer", "enhance_all_cards"),
    "closure-checker": ("src.closure_checker", "check_evidence_closure"),
    "patch-planner": ("src.patch_planner", "create_patch_plan"),
    "patch-executor": ("src.patch_executor", "execute_patch"),
    "validator": ("src.validator", "validate_patch"),
}


class WorkerRuntime:
    """Execute selected workers with deterministic behavior."""

    async def run(self, spec: WorkerSpec, workspace_dir: str, instance_id: str) -> WorkerExecutionResult:
        module_name, func_name = _EXECUTOR_MAP.get(spec.worker_id, ("", ""))
        if not module_name:
            return WorkerExecutionResult(worker_id=spec.worker_id, success=False, error="no_executor")

        try:
            module = importlib.import_module(module_name)
            executor = getattr(module, func_name)
        except (ImportError, AttributeError) as exc:
            return WorkerExecutionResult(worker_id=spec.worker_id, success=False, error=str(exc))

        try:
            if asyncio.iscoroutinefunction(executor):
                raw = await executor(workspace_dir, instance_id)
            else:
                raw = executor(workspace_dir, instance_id)
            outputs = raw if isinstance(raw, dict) else {"result": str(raw)}
            return WorkerExecutionResult(worker_id=spec.worker_id, success=True, outputs=outputs)
        except Exception as exc:
            return WorkerExecutionResult(worker_id=spec.worker_id, success=False, error=str(exc))
