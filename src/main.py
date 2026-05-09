"""
CLI entry point for the Evidence-Closure-Aware Repair Harness.

Accepts a SWE-bench Pro instance and runs the full pipeline:
evidence collection -> patch planning -> patch generation -> git diff output.

Usage:

  # By dataset index (loads from HuggingFace):
  python -m src.main --index 0 --repo-dir /app

  # By instance_id (loads from HuggingFace):
  python -m src.main --instance-id django__django-16046 --repo-dir /app

  # From a local instance metadata JSON:
    python -m src.main --instance-json workdir/swe_issue_001/artifacts/instance_metadata.json \
      --repo-dir workdir/swe_issue_001/repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.artifacts import instance_to_artifact_text
from src.orchestrator.engine import run_orchestrator


def load_instance_from_dataset(
    index: int | None = None,
    instance_id: str | None = None,
) -> dict:
    """Load an instance from the HuggingFace SWE-bench Pro dataset."""
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("ERROR: 'datasets' library is not installed.")
        print("       Run: pip install datasets")
        sys.exit(1)

    print("Loading ScaleAI/SWE-bench_Pro dataset...")
    dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")

    if index is not None:
        instance = dataset[index]
    elif instance_id is not None:
        instance = None
        for row in dataset:
            if row["instance_id"] == instance_id:
                instance = row
                break
        if instance is None:
            print(f"ERROR: instance_id '{instance_id}' not found in dataset.")
            sys.exit(1)
    else:
        raise ValueError("Either --index or --instance-id must be provided.")

    print(f"Loaded instance: {instance.get('instance_id', '<unknown>')}")
    return dict(instance)


def prepare_repo(repo_dir: Path, base_commit: str) -> None:
    """Ensure repo is at clean base_commit state.

    This prevents the system from generating patches based on previously
    modified code by resetting the working directory to a clean state.

    Args:
        repo_dir: Path to the repository root.
        base_commit: Git commit hash to reset to.
    """
    print(f"[repo-init] Resetting repo to clean state at {base_commit[:8]}...")

    # Reset to base_commit (discards any uncommitted changes)
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", base_commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"WARNING: git reset failed: {result.stderr}")
        print("Continuing anyway...")

    # Clean untracked files and directories
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "clean", "-fd"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"WARNING: git clean failed: {result.stderr}")
        print("Continuing anyway...")

    # Ensure we're on the base_commit (detached HEAD is fine)
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", base_commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"WARNING: git checkout failed: {result.stderr}")
        print("Continuing anyway...")

    print(f"[repo-init] Repo prepared at {base_commit[:8]}")


def write_prediction(
    output_dir: Path,
    instance_id: str,
    patch_path: Path | None,
) -> Path:
    """Write a SWE-bench compatible prediction JSON file."""
    patch_text = ""
    if patch_path is not None and patch_path.exists():
        patch_text = patch_path.read_text(encoding="utf-8")

    pred = {
        "instance_id": instance_id,
        "model_patch": patch_text,
    }

    pred_path = output_dir / "prediction.json"
    pred_path.write_text(json.dumps(pred, indent=2), encoding="utf-8")
    print(f"Prediction written -> {pred_path}")
    return pred_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the repair harness on a SWE-bench Pro instance.",
    )

    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument(
        "--index", type=int, default=None,
        help="Dataset index to load from ScaleAI/SWE-bench_Pro.",
    )
    src_group.add_argument(
        "--instance-id", type=str, default=None,
        help="Instance ID to look up in ScaleAI/SWE-bench_Pro.",
    )
    src_group.add_argument(
        "--instance-json", type=str, default=None,
        help="Path to a local instance_metadata.json file.",
    )

    parser.add_argument(
        "--repo-dir", type=str, required=True,
        help="Path to the repository root (e.g. /app in Docker).",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help=(
            "Output directory. Defaults to workdir/<issue_name>/outputs for "
            "--instance-json mode, otherwise workdir/<instance_id>/outputs."
        ),
    )

    args = parser.parse_args()

    # --- Load instance ---
    if args.instance_json:
        path = Path(args.instance_json)
        if not path.exists():
            print(f"ERROR: Instance JSON not found: {path}")
            sys.exit(1)
        instance = json.loads(path.read_text(encoding="utf-8"))
    elif args.index is not None or args.instance_id:
        instance = load_instance_from_dataset(
            index=args.index,
            instance_id=args.instance_id if args.index is None else None,
        )
    else:
        print("ERROR: Provide one of --index, --instance-id, or --instance-json.")
        sys.exit(1)

    instance_id = instance["instance_id"]
    base_commit = instance.get("base_commit", "")
    repo_dir = Path(args.repo_dir)
    if not repo_dir.exists():
        print(f"ERROR: repo_dir not found: {repo_dir}")
        sys.exit(1)

    # --- Prepare repo: reset to clean base_commit state ---
    if base_commit:
        prepare_repo(repo_dir, base_commit)
    else:
        print("WARNING: No base_commit found in instance metadata.")
        print("         Repo will not be reset. This may cause incorrect patches.")

    # --- Output directory ---
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        if args.instance_json:
            instance_json_path = Path(args.instance_json).resolve()
            # Preferred layout: workdir/<issue_name>/artifacts/instance_metadata.json
            if instance_json_path.parent.name == "artifacts":
                issue_dir = instance_json_path.parent.parent
            else:
                issue_dir = instance_json_path.parent
            output_dir = issue_dir / "outputs"
        else:
            output_dir = Path("workdir") / instance_id / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Schema-version guard: refuse stale evidence.json ---
    existing_evidence = output_dir / "evidence.json"
    if existing_evidence.exists():
        try:
            existing = json.loads(existing_evidence.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict) and existing.get("schema_version") != "v2":
            print(
                f"ERROR: existing {existing_evidence} is missing schema_version='v2'. "
                "Phase 16 does NOT auto-migrate old-schema artifacts. "
                "Move or delete the file and re-run to regenerate."
            )
            sys.exit(1)

    # --- Convert instance to artifact text ---
    artifact_text = instance_to_artifact_text(instance)

    print(f"=== Repair Harness ===")
    print(f"Instance ID : {instance_id}")
    print(f"Repo dir    : {repo_dir}")
    print(f"Output dir  : {output_dir}")
    print()

    # --- Run orchestrator ---
    evidence_path = run_orchestrator(
        issue_id=instance_id,
        repo_dir=repo_dir,
        artifact_text=artifact_text,
        output_dir=output_dir,
    )

    print(f"\n=== COMPLETE ===")
    print(f"Evidence JSON: {evidence_path}")

    # --- Write prediction for SWE-bench eval ---
    patch_path = output_dir / "patch.diff"
    write_prediction(
        output_dir=output_dir,
        instance_id=instance_id,
        patch_path=patch_path if patch_path.exists() else None,
    )


if __name__ == "__main__":
    main()
