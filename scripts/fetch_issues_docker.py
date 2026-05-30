"""
Fetch SWE-bench Pro cases using Docker build to clone repos.

For each issue, builds the base Dockerfile (which clones the repo),
then copies /app out of the container into workdir/swe_issue_NNN/repo/.

Usage:
    python scripts/fetch_issues_docker.py --start 10 --count 5 --start-label 11
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DOCKER = "C:/Program Files/Docker/Docker/resources/bin/docker.exe"
EVAL_DIR = Path("eval/SWE-bench_Pro-os")
DOCKERHUB_USER = "jefzda"


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def docker(*args, capture=True, timeout=600) -> subprocess.CompletedProcess:
    return run(
        [DOCKER] + list(args),
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def load_instances(start: int, count: int) -> list[dict]:
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("ERROR: pip install datasets")
        sys.exit(1)
    print("Loading ScaleAI/SWE-bench_Pro dataset...")
    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    instances = []
    for i in range(start, start + count):
        inst = dict(ds[i])
        print(f"  [{i}] {inst['instance_id']}")
        instances.append(inst)
    return instances


def get_image_tag(instance: dict) -> str:
    """Return the full Docker image tag for an instance."""
    dockerhub_tag = instance.get("dockerhub_tag", "")
    return f"{DOCKERHUB_USER}/sweap-images:{dockerhub_tag}"


def pull_image(image_tag: str) -> bool:
    """Pull a Docker image, returns True on success."""
    print(f"    pulling {image_tag} ...")
    r = run([DOCKER, "pull", image_tag], capture_output=False, timeout=600)
    return r.returncode == 0


def extract_repo_from_image(image_tag: str, dest_dir: Path) -> bool:
    """Extract /app from a Docker image to dest_dir.

    Runs a container that dereferences symlinks into /app_copy, then docker cp.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Clean up any leftover container
    run([DOCKER, "rm", "-f", "swe_extract_tmp"], capture_output=True, text=True, timeout=15)

    # Step 1: run a container that copies /app to /app_copy with symlinks resolved
    # Use --entrypoint to override and run synchronously
    print(f"    dereferencing symlinks in container...")
    r = run(
        [DOCKER, "run", "--name", "swe_extract_tmp",
         "--entrypoint", "/bin/bash",
         image_tag,
         "-c", "cp -rL /app /app_copy"],
        capture_output=True, text=True, timeout=300,
    )
    # cp -rL may fail if there are broken symlinks; that's OK as long as /app_copy exists
    print(f"    cp -rL exit={r.returncode}")

    try:
        # Step 2: docker cp from the container
        print(f"    copying /app_copy -> {dest_dir}")
        r2 = run(
            [DOCKER, "cp", "swe_extract_tmp:/app_copy/.", str(dest_dir)],
            capture_output=True, text=True, timeout=300,
        )
        if r2.returncode != 0:
            print(f"    /app_copy failed ({r2.stderr[:150]})")
            return False

        if not dest_dir.exists() or not any(dest_dir.iterdir()):
            print(f"    ERROR: dest_dir is empty after extraction")
            return False

        print(f"    extracted to {dest_dir}")
        return True
    finally:
        run([DOCKER, "rm", "-f", "swe_extract_tmp"],
            capture_output=True, text=True, timeout=30)


def setup_issue(label: int, instance: dict, workdir: Path) -> None:
    issue_dir = workdir / f"swe_issue_{label:03d}"
    artifacts_dir = issue_dir / "artifacts"
    repo_dir = issue_dir / "repo"

    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Save metadata
    metadata_path = artifacts_dir / "instance_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(instance, f, indent=2, ensure_ascii=False)
    print(f"  saved metadata -> {metadata_path}")

    # Find or pull image
    image_tag = get_image_tag(instance)
    print(f"  image: {image_tag}")

    # Check if repo already exists
    if repo_dir.exists() and (repo_dir / ".git").exists():
        print(f"  repo already exists at {repo_dir}, skipping")
        return

    # Pull image
    if not pull_image(image_tag):
        print(f"  ERROR: failed to pull image {image_tag}")
        return

    # Extract repo
    if not extract_repo_from_image(image_tag, repo_dir):
        print(f"  ERROR: failed to extract repo for {instance['instance_id']}")
        return

    # Reset to base_commit
    base_commit = instance["base_commit"]
    print(f"  resetting to base_commit {base_commit[:8]}")
    r = run(
        [DOCKER, "run", "--rm", "-v", f"{repo_dir.resolve()}:/app",
         "alpine/git", "reset", "--hard", base_commit],
        capture_output=True, text=True, timeout=60,
    )
    # Fallback: use git directly if available
    git_r = subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", base_commit],
        capture_output=True, text=True,
    )
    if git_r.returncode == 0:
        subprocess.run(
            ["git", "-C", str(repo_dir), "clean", "-fd"],
            capture_output=True, text=True,
        )
        print(f"  reset to {base_commit[:8]} OK")
    else:
        print(f"  WARNING: could not reset to base_commit: {git_r.stderr}")

    print(f"  done: {issue_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=10)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--start-label", type=int, default=11)
    parser.add_argument("--workdir", type=str, default="workdir")
    parser.add_argument("--only", type=int, default=None,
                        help="Only process this label number (e.g. 11)")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    instances = load_instances(args.start, args.count)

    for i, instance in enumerate(instances):
        label = args.start_label + i
        if args.only is not None and label != args.only:
            continue
        print(f"\n[{label:03d}] {instance['instance_id']}")
        setup_issue(label, instance, workdir)

    print("\nAll done.")


if __name__ == "__main__":
    main()
