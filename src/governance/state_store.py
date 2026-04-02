"""State persistence for Orchestrator."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

from ..contracts import WorkflowState


class StateStore:
    """Persist workflow states with atomic writes and versioning."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _workflow_dir(self, workflow_id: str) -> Path:
        path = self.root_dir / workflow_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _latest_file(self, workflow_id: str) -> Path:
        return self._workflow_dir(workflow_id) / "latest.json"

    def _version_file(self, workflow_id: str, version: int) -> Path:
        return self._workflow_dir(workflow_id) / f"state_v{version}.json"

    def save(self, workflow_state: WorkflowState, workflow_id: Optional[str] = None) -> Path:
        """Save state atomically and create a versioned snapshot."""

        wf_id = workflow_id or workflow_state.instance_id
        wf_dir = self._workflow_dir(wf_id)
        latest = self._latest_file(wf_id)

        version = self._next_version(wf_id)
        payload = workflow_state.model_dump()
        payload["updated_at"] = datetime.utcnow().isoformat()
        payload["schema_version"] = payload.get("schema_version", "1.0")

        version_file = self._version_file(wf_id, version)
        self._atomic_write(version_file, payload)
        self._atomic_write(latest, payload)

        index_path = wf_dir / "index.json"
        index_payload = {
            "workflow_id": wf_id,
            "latest_version": version,
            "updated_at": payload["updated_at"],
        }
        self._atomic_write(index_path, index_payload)
        return version_file

    def load(self, workflow_id: str, version: Optional[int] = None) -> WorkflowState:
        """Load latest or specific version state."""

        target = self._latest_file(workflow_id) if version is None else self._version_file(workflow_id, version)
        if not target.exists():
            raise FileNotFoundError(f"State file not found: {target}")
        data = json.loads(target.read_text(encoding="utf-8"))
        return WorkflowState(**data)

    def _next_version(self, workflow_id: str) -> int:
        wf_dir = self._workflow_dir(workflow_id)
        versions = []
        for child in wf_dir.glob("state_v*.json"):
            suffix = child.stem.replace("state_v", "")
            if suffix.isdigit():
                versions.append(int(suffix))
        return (max(versions) + 1) if versions else 1

    def _atomic_write(self, target: Path, payload: dict) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=str(target.parent)) as tmp:
            tmp.write(json.dumps(payload, ensure_ascii=False, indent=2))
            temp_path = Path(tmp.name)
        temp_path.replace(target)
