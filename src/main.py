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
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.artifacts import instance_to_artifact_text
from src.agents._cost_tracker import get_totals as get_cost_totals, reset as reset_cost_tracker
from src.memory import ensure_running as ensure_ltm_running
from src.output_paths import default_output_dir, model_output_dir_name
from src.orchestrator.engine import (
    _delete_checkpoint,
    _load_checkpoint,
    _model_runtime_metadata,
    run_orchestrator,
    run_pipeline_from_checkpoint,
)


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

    # Disable fileMode tracking — Windows/NTFS cannot preserve Unix execute
    # bits, causing spurious mode-only diffs that pollute patch.diff and can
    # break git stash pop during build verification.
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "core.fileMode", "false"],
        capture_output=True, text=True, check=False,
    )

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


_TERMINAL_ARTIFACTS = (
    "patch.diff",
    "patch_outcome.json",
    "prediction.json",
    "build_verification.json",
    "run_metrics.json",
    "working_memory.json",
    "patch_plan.json",
    "patch_failures.log",
)


def clear_terminal_artifacts(output_dir: Path) -> None:
    """Remove stale terminal artifacts before starting or resuming a run."""
    removed: list[str] = []
    for name in _TERMINAL_ARTIFACTS:
        path = output_dir / name
        if not path.exists():
            continue
        path.unlink()
        removed.append(name)
    if removed:
        print(
            "[outputs] cleared stale terminal artifacts: "
            + ", ".join(sorted(removed)),
            flush=True,
        )


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
            "Output directory. Defaults to workdir/<issue_name>/outputs_<model> "
            "for --instance-json mode, otherwise workdir/<instance_id>/outputs_<model>."
        ),
    )
    parser.add_argument(
        "--force-restart", action="store_true",
        help="Ignore any existing checkpoint and start the pipeline from scratch.",
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

    # --- Long-term memory: ensure experience_server is running -------------
    # Required dependency. Blocks until /health is OK; first launch may take
    # 1-3 minutes while the Qwen embedding model loads (and downloads on
    # cold cache).
    ensure_ltm_running()

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
            output_dir = default_output_dir(issue_dir)
        else:
            output_dir = default_output_dir(Path("workdir") / instance_id)
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

    # --- Run orchestrator (with checkpoint resume support) ---
    reset_cost_tracker()
    run_start_ts = time.monotonic()
    run_start_iso = datetime.now(timezone.utc).isoformat()

    checkpoint = _load_checkpoint(output_dir)
    if checkpoint and not args.force_restart:
        saved_runtime = checkpoint.get("runtime")
        current_runtime = _model_runtime_metadata()
        if saved_runtime and saved_runtime != current_runtime:
            print(
                "[resume] Checkpoint runtime does not match current model "
                f"runtime. checkpoint={saved_runtime}, current={current_runtime}. "
                "Use --force-restart to start a fresh run.",
                flush=True,
            )
            sys.exit(1)
    clear_terminal_artifacts(output_dir)
    if checkpoint and not args.force_restart:
        state_name = checkpoint.get("pipeline_state", "?")
        saved_at = checkpoint.get("saved_at", "?")
        print(
            f"[resume] Checkpoint found (state={state_name}, saved={saved_at}). "
            "Resuming from last save point.",
            flush=True,
        )
        evidence_path = asyncio.run(run_pipeline_from_checkpoint(
            issue_id=instance_id,
            repo_dir=repo_dir,
            output_dir=output_dir,
            checkpoint=checkpoint,
        ))
    else:
        if checkpoint and args.force_restart:
            print("[resume] --force-restart: ignoring existing checkpoint, starting fresh.")
            _delete_checkpoint(output_dir)
        evidence_path = run_orchestrator(
            issue_id=instance_id,
            repo_dir=repo_dir,
            artifact_text=artifact_text,
            output_dir=output_dir,
            problem_statement=instance.get("problem_statement", "") or artifact_text,
        )

    run_end_iso = datetime.now(timezone.utc).isoformat()
    wall_clock_seconds = time.monotonic() - run_start_ts

    print(f"\n=== COMPLETE ===")
    print(f"Evidence JSON: {evidence_path}")

    # --- Write run metrics (timing + token cost) ---
    cost = get_cost_totals()
    runtime = _model_runtime_metadata()
    metrics = {
        "instance_id": instance_id,
        "model_backend": runtime.get("model_backend", "unknown"),
        "model": runtime.get("model") or "unknown",
        "output_dir_name": model_output_dir_name(runtime.get("model") or None),
        "run_start_utc": run_start_iso,
        "run_end_utc": run_end_iso,
        "wall_clock_seconds": round(wall_clock_seconds, 1),
        **cost,
    }
    metrics_path = output_dir / "run_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Run metrics  -> {metrics_path}")

    # --- Write prediction for SWE-bench eval ---
    patch_path = output_dir / "patch.diff"
    write_prediction(
        output_dir=output_dir,
        instance_id=instance_id,
        patch_path=patch_path if patch_path.exists() else None,
    )


if __name__ == "__main__":
    main()
