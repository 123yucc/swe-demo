"""Shared helpers for worker implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def resolve_instance_dir(workspace_dir: str, instance_id: str) -> Path:
    base = Path(workspace_dir)
    if base.name == instance_id:
        return base
    return base / instance_id


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_first_existing(root: Path, names: Iterable[str]) -> Optional[str]:
    for name in names:
        candidate = root / name
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return None
