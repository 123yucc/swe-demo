"""
CLI entry point for the Evidence-Closure-Aware Repair Harness.

Usage:
    python -m src.main <issue_id> <artifacts_dir> <repo_dir>

Example:
    python -m src.main face_recognition_issue_001 \
        workdir/face_recognition_issue_001/artifacts \
        workdir/face_recognition_issue_001/repo
"""

import sys
from pathlib import Path

from src.orchestrator.engine import run_orchestrator


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python -m src.main <issue_id> <artifacts_dir> <repo_dir>")
        sys.exit(1)

    issue_id = sys.argv[1]
    artifacts_dir = Path(sys.argv[2])
    repo_dir = Path(sys.argv[3])

    if not artifacts_dir.exists():
        print(f"Error: artifacts_dir not found: {artifacts_dir}")
        sys.exit(1)
    if not repo_dir.exists():
        print(f"Error: repo_dir not found: {repo_dir}")
        sys.exit(1)

    print(f"Starting evidence-closure loop for issue: {issue_id}")
    print(f"  Artifacts: {artifacts_dir}")
    print(f"  Repo:      {repo_dir}")
    print()

    evidence_path = run_orchestrator(issue_id, artifacts_dir, repo_dir)
    print(f"=== EVIDENCE COLLECTION COMPLETE ===")
    print(f"Evidence JSON: {evidence_path}")


if __name__ == "__main__":
    main()
