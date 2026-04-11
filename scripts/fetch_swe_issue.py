#!/usr/bin/env python3
"""
Fetch the first SWE-bench Pro instance and set up the workdir structure.

This script:
1. Loads the first instance from the ScaleAI/SWE-bench_Pro dataset on HuggingFace.
2. Creates workdir/swe_issue_001/artifacts/ with 4 Markdown artifact files
   required by the Evidence-Closure-Aware Repair Harness.
3. Clones the repository at base_commit into workdir/swe_issue_001/repo/.
4. Pulls the Docker image and saves it as a .tar file under workdir/swe_issue_001/.

Usage:
    python scripts/fetch_swe_issue.py [--workdir WORKDIR] [--no-docker] [--no-repo]

Options:
    --workdir   Root directory for output (default: workdir)
    --no-docker Skip Docker image pull/save step
    --no-repo   Skip git clone step
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path



DATASET_NAME = "ScaleAI/SWE-bench_Pro"
DATASET_SPLIT = "test"
DOCKER_IMAGE_PREFIX = "jefzda/sweap-images"
ISSUE_DIR_NAME = "swe_issue_001"


def load_first_instance() -> dict:
    """Load the first instance from the SWE-bench Pro dataset."""
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("ERROR: 'datasets' library is not installed.")
        print("       Run: pip install datasets")
        sys.exit(1)

    print(f"Loading dataset '{DATASET_NAME}' (split='{DATASET_SPLIT}')...")
    dataset = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    instance: dict = dataset[0]
    print(f"Loaded instance: {instance.get('instance_id', '<unknown>')}")
    return instance


def clone_repo(instance: dict, repo_dir: Path) -> None:
    """Clone the repository at base_commit into repo_dir."""
    repo_slug = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")

    if not repo_slug:
        print("WARNING: No 'repo' field in instance — skipping clone.")
        return

    repo_url = f"https://github.com/{repo_slug}.git"
    print(f"Cloning {repo_url} into {repo_dir} ...")

    repo_dir.mkdir(parents=True, exist_ok=True)

    # Clone (shallow first, then fetch the specific commit if needed)
    result = subprocess.run(
        ["git", "clone", "--quiet", repo_url, str(repo_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: git clone failed:\n{result.stderr}")
        sys.exit(1)

    if base_commit:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", "--quiet", base_commit],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"WARNING: Could not checkout {base_commit}:\n{result.stderr}")
        else:
            print(f"  Checked out commit: {base_commit}")


def pull_and_save_docker_image(instance: dict, issue_dir: Path) -> None:
    """Pull the Docker image for this instance and save it as a .tar file."""
    docker_tag = instance.get("dockerhub_tag", "")
    if not docker_tag:
        print("WARNING: No 'dockerhub_tag' field in instance — skipping Docker pull.")
        return

    full_image = f"{DOCKER_IMAGE_PREFIX}:{docker_tag}"
    tar_filename = f"docker_image_{docker_tag.replace('/', '_').replace(':', '_')}.tar"
    tar_path = issue_dir / tar_filename

    print(f"Pulling Docker image: {full_image} ...")
    pull_result = subprocess.run(
        ["docker", "pull", full_image],
        capture_output=False,
    )
    if pull_result.returncode != 0:
        print(f"ERROR: docker pull failed for {full_image}")
        sys.exit(1)

    print(f"Saving image to {tar_path} ...")
    save_result = subprocess.run(
        ["docker", "save", "-o", str(tar_path), full_image],
        capture_output=False,
    )
    if save_result.returncode != 0:
        print(f"ERROR: docker save failed.")
        sys.exit(1)

    print(f"  Saved: {tar_path}")


def save_instance_metadata(instance: dict, issue_dir: Path) -> None:
    """Save a JSON file with all instance metadata for reference."""
    meta_path = issue_dir / "instance_metadata.json"
    # Serialize only JSON-safe fields (convert non-serializable items to str)
    safe_meta = {}
    for k, v in instance.items():
        try:
            json.dumps(v)
            safe_meta[k] = v
        except (TypeError, ValueError):
            safe_meta[k] = str(v)

    meta_path.write_text(json.dumps(safe_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved instance metadata: {meta_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch first SWE-bench Pro instance and set up workdir structure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--workdir",
        default="workdir",
        help="Root directory for output (default: workdir)",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Skip Docker image pull/save step",
    )
    parser.add_argument(
        "--no-repo",
        action="store_true",
        help="Skip git clone step",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir)
    issue_dir = workdir / ISSUE_DIR_NAME
    artifacts_dir = issue_dir / "artifacts"
    repo_dir = issue_dir / "repo"

    print("=== SWE-bench Pro Issue Setup ===")
    print(f"Issue directory : {issue_dir}")
    print(f"Artifacts       : {artifacts_dir}")
    print(f"Repo            : {repo_dir}")
    print()

    instance = load_first_instance()
    instance_id = instance.get("instance_id", "<unknown>")
    print(f"\nInstance ID : {instance_id}")
    print(f"Repository  : {instance.get('repo', '')}")
    print(f"Base commit : {instance.get('base_commit', '')}")
    print(f"Docker tag  : {instance.get('dockerhub_tag', '')}")
    print()

    print("--- Writing issue file ---")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    issue_path = artifacts_dir / "issue.md"
    issue_path.write_text(instance.get("problem_statement", ""), encoding="utf-8")
    print(f"  Written: {issue_path}")

    print("\n--- Saving instance metadata ---")
    save_instance_metadata(instance, issue_dir)

    if not args.no_repo:
        print("\n--- Cloning repository ---")
        clone_repo(instance, repo_dir)
    else:
        print("\n[Skipping repo clone (--no-repo)]")

    if not args.no_docker:
        print("\n--- Pulling Docker image ---")
        pull_and_save_docker_image(instance, issue_dir)
    else:
        print("\n[Skipping Docker pull (--no-docker)]")

    print("\n=== Setup complete ===")
    print(f"Run the harness with:")
    print(
        f"  python -m src.main --instance-json {issue_dir / 'instance_metadata.json'} "
        f"--repo-dir {repo_dir}"
    )


if __name__ == "__main__":
    main()
