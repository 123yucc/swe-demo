"""Working-memory indexing helpers for artifacts/code/tests."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List


def build_workspace_index(instance_dir: str) -> Dict[str, List[str]]:
    """Create lightweight index for issue/code/tests files."""
    root = Path(instance_dir)
    artifacts = [str(p.relative_to(root)) for p in (root / "artifacts").rglob("*") if p.is_file()] if (root / "artifacts").exists() else []
    repo = [str(p.relative_to(root)) for p in (root / "repo").rglob("*") if p.is_file()] if (root / "repo").exists() else []
    tests = [path for path in artifacts if "/tests/" in path.replace("\\", "/") or path.startswith("artifacts/tests/")]
    return {"artifacts": artifacts, "repo": repo, "tests": tests}
