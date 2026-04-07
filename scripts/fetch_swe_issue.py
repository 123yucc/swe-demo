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


def build_problem_statement_md(instance: dict) -> str:
    """Build problem_statement.md content from dataset instance."""
    problem_statement = instance.get("problem_statement", "").strip()
    instance_id = instance.get("instance_id", "")
    repo = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")

    lines = [
        f"# Problem Statement",
        f"",
        f"**Instance ID:** `{instance_id}`  ",
        f"**Repository:** `{repo}`  ",
        f"**Base Commit:** `{base_commit}`  ",
        f"",
        problem_statement,
    ]
    return "\n".join(lines)


def build_requirements_md(instance: dict) -> str:
    """Build requirements.md content from dataset instance."""
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    pass_to_pass = instance.get("PASS_TO_PASS", [])

    # Normalize: the dataset may store these as JSON strings or lists
    if isinstance(fail_to_pass, str):
        try:
            fail_to_pass = json.loads(fail_to_pass)
        except json.JSONDecodeError:
            fail_to_pass = [fail_to_pass]
    if isinstance(pass_to_pass, str):
        try:
            pass_to_pass = json.loads(pass_to_pass)
        except json.JSONDecodeError:
            pass_to_pass = [pass_to_pass]

    fail_lines = "\n".join(f"- `{t}`" for t in fail_to_pass) if fail_to_pass else "_None specified_"
    pass_lines = "\n".join(f"- `{t}`" for t in pass_to_pass) if pass_to_pass else "_None specified_"

    lines = [
        "# Requirements",
        "",
        "## Tests That Must Pass After the Fix (FAIL_TO_PASS)",
        "",
        "The following tests are currently failing and must pass once the fix is applied:",
        "",
        fail_lines,
        "",
        "## Tests That Must Continue to Pass (PASS_TO_PASS)",
        "",
        "The following tests are currently passing and must not regress:",
        "",
        pass_lines,
    ]
    return "\n".join(lines)


def build_new_interfaces_md(instance: dict) -> str:
    """Build new_interfaces.md content from dataset instance."""
    # SWE-bench Pro does not typically include new interface specifications.
    # Provide a structured placeholder so the parser agent handles it gracefully.
    instance_id = instance.get("instance_id", "")
    lines = [
        "# New Interfaces",
        "",
        f"This document covers new or modified public interfaces required by the fix "
        f"for `{instance_id}`.",
        "",
        "No explicit new interface specifications are provided for this SWE-bench Pro "
        "instance. The fix is expected to restore existing behaviour without introducing "
        "new public APIs.",
    ]
    return "\n".join(lines)


def build_expected_and_current_behavior_md(instance: dict) -> str:
    """Build expected_and_current_behavior.md content from dataset instance."""
    problem_statement = instance.get("problem_statement", "").strip()
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if isinstance(fail_to_pass, str):
        try:
            fail_to_pass = json.loads(fail_to_pass)
        except json.JSONDecodeError:
            fail_to_pass = [fail_to_pass]

    failing_tests = "\n".join(f"- `{t}`" for t in fail_to_pass) if fail_to_pass else "_See problem statement_"

    lines = [
        "# Expected and Current Behavior",
        "",
        "## Current (Broken) Behavior",
        "",
        "The repository at the base commit exhibits the bug described in the problem statement.",
        "The following tests are currently failing due to this bug:",
        "",
        failing_tests,
        "",
        "## Problem Description",
        "",
        problem_statement,
        "",
        "## Expected Behavior",
        "",
        "After the fix is applied:",
        "- All tests listed in the FAIL_TO_PASS section of `requirements.md` must pass.",
        "- No tests listed in the PASS_TO_PASS section of `requirements.md` may regress.",
        "- The repository behaviour must conform to the description in `problem_statement.md`.",
    ]
    return "\n".join(lines)


def write_artifacts(artifacts_dir: Path, instance: dict) -> None:
    """Write the 4 Markdown artifact files to artifacts_dir."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "problem_statement.md": build_problem_statement_md(instance),
        "requirements.md": build_requirements_md(instance),
        "new_interfaces.md": build_new_interfaces_md(instance),
        "expected_and_current_behavior.md": build_expected_and_current_behavior_md(instance),
    }

    for filename, content in files.items():
        path = artifacts_dir / filename
        path.write_text(content, encoding="utf-8")
        print(f"  Written: {path}")


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

    print("--- Writing artifact files ---")
    write_artifacts(artifacts_dir, instance)

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
        f"  python -m src.main {instance_id} "
        f"{artifacts_dir} {repo_dir}"
    )


if __name__ == "__main__":
    main()
