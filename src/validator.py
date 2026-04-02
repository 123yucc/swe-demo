"""Phase6 validator worker implementation."""

from __future__ import annotations

from typing import Any, Dict

from .workers._common import resolve_instance_dir, save_json


def validate_patch(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    patch_file = instance_dir / "patch" / "changes.pred"
    valid = patch_file.exists()
    report = {"valid": valid, "reason": "patch file exists" if valid else "missing patch file"}
    save_json(instance_dir / "patch" / "validation_report.json", report)
    return report
