"""Workspace bootstrap helpers."""

from pathlib import Path


def setup_workspace(instance_id: str, base_dir: str = "workdir") -> str:
    """Create and return workspace directory for one instance."""
    workspace = Path(base_dir) / instance_id
    workspace.mkdir(parents=True, exist_ok=True)

    (workspace / "artifacts").mkdir(exist_ok=True)
    (workspace / "artifacts" / "tests" / "fail2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts" / "tests" / "pass2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "evidence").mkdir(exist_ok=True)
    (workspace / "evidence" / "card_versions").mkdir(parents=True, exist_ok=True)
    (workspace / "repo").mkdir(exist_ok=True)

    (workspace / ".memory" / "longterm").mkdir(parents=True, exist_ok=True)
    (workspace / ".workflow").mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(exist_ok=True)
    (workspace / "plan").mkdir(exist_ok=True)

    return str(workspace)
