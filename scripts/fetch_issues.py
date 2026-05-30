"""
Fetch SWE-bench Pro cases and set up workdir structure.

Usage:
    python scripts/fetch_issues.py --start 10 --count 5 --start-label 11
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def load_instances(start: int, count: int) -> list[dict]:
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("ERROR: 'datasets' library not installed. Run: pip install datasets")
        sys.exit(1)

    print("Loading ScaleAI/SWE-bench_Pro dataset...")
    dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    instances = []
    for i in range(start, start + count):
        inst = dict(dataset[i])
        print(f"  [{i}] {inst['instance_id']}")
        instances.append(inst)
    return instances


def clone_repo(repo: str, base_commit: str, repo_dir: Path) -> None:
    if repo_dir.exists() and (repo_dir / ".git").exists():
        print(f"    repo already exists, resetting to {base_commit[:8]}")
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "origin"],
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "reset", "--hard", base_commit],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "clean", "-fd"],
            check=True,
        )
        return

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    print(f"    cloning {url} ...")
    result = subprocess.run(
        ["git", "clone", "--quiet", url, str(repo_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"    ERROR cloning: {result.stderr}")
        raise RuntimeError(f"git clone failed for {repo}")

    print(f"    resetting to {base_commit[:8]}")
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", base_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "clean", "-fd"],
        check=True,
    )


def setup_issue(label: int, instance: dict, workdir: Path) -> None:
    issue_dir = workdir / f"swe_issue_{label:03d}"
    artifacts_dir = issue_dir / "artifacts"
    repo_dir = issue_dir / "repo"

    artifacts_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = artifacts_dir / "instance_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(instance, f, indent=2, ensure_ascii=False)
    print(f"  saved metadata -> {metadata_path}")

    print(f"  setting up repo for {instance['repo']} @ {instance['base_commit'][:8]}")
    clone_repo(instance["repo"], instance["base_commit"], repo_dir)
    print(f"  done: {issue_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SWE-bench Pro cases")
    parser.add_argument("--start", type=int, default=10, help="Dataset start index (0-based)")
    parser.add_argument("--count", type=int, default=5, help="Number of cases to fetch")
    parser.add_argument("--start-label", type=int, default=11, help="Starting issue label number")
    parser.add_argument("--workdir", type=str, default="workdir", help="Output workdir path")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    instances = load_instances(args.start, args.count)

    for i, instance in enumerate(instances):
        label = args.start_label + i
        print(f"\n[{label:03d}] {instance['instance_id']}")
        setup_issue(label, instance, workdir)

    print("\nAll done.")


if __name__ == "__main__":
    main()
