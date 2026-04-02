"""Phase5 patch executor worker implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .workers._common import load_json, resolve_instance_dir, save_json


def execute_patch(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    plan_path = instance_dir / "plan" / "patch_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError("patch_plan.json missing")

    _ = load_json(plan_path)
    patch_dir = instance_dir / "patch"
    patch_dir.mkdir(parents=True, exist_ok=True)
    pred_path = patch_dir / "changes.pred"
    pred_path.write_text("# placeholder patch\n", encoding="utf-8")

    result = {"applied": True, "patch_file": str(pred_path)}
    save_json(patch_dir / "patch_result.json", result)
    return result
