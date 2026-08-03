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

_ensured_mirrors: set[Path] = set()


def load_instances(
    start: int,
    count: int | None,
    source_jsonl: Path | None = None,
) -> list[dict]:
    if source_jsonl is not None:
        rows = [
            json.loads(line)
            for line in source_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        stop = len(rows) if count is None else start + count
        return rows[start:stop]

    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print("ERROR: 'datasets' library not installed. Run: pip install datasets")
        sys.exit(1)

    print("Loading ScaleAI/SWE-bench_Pro dataset...")
    dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    instances = []
    stop = len(dataset) if count is None else start + count
    for i in range(start, stop):
        inst = dict(dataset[i])
        print(f"  [{i}] {inst['instance_id']}")
        instances.append(inst)
    return instances


def _mirror_path(repo: str, repo_cache: Path) -> Path:
    return repo_cache / (repo.replace("/", "--") + ".git")


def _commit_exists(repo_path: Path, commit: str, *, bare: bool) -> bool:
    prefix = (
        ["git", "--git-dir", str(repo_path)]
        if bare
        else ["git", "-C", str(repo_path)]
    )
    result = subprocess.run(
        [*prefix, "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def ensure_repo_mirror(
    repo: str,
    repo_cache: Path,
    required_commit: str | None = None,
) -> Path:
    mirror = _mirror_path(repo, repo_cache)
    if (
        mirror in _ensured_mirrors
        and (
            required_commit is None
            or _commit_exists(mirror, required_commit, bare=True)
        )
    ):
        return mirror
    url = f"https://github.com/{repo}.git"
    if mirror.exists():
        if required_commit is None or not _commit_exists(
            mirror, required_commit, bare=True
        ):
            subprocess.run(
                ["git", "-C", str(mirror), "fetch", "--quiet", "--prune", "origin"],
                check=False,
            )
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--mirror", "--quiet", url, str(mirror)], check=True)
    if required_commit is not None and not _commit_exists(
        mirror, required_commit, bare=True
    ):
        raise RuntimeError(
            f"repository cache for {repo} does not contain {required_commit}"
        )
    _ensured_mirrors.add(mirror)
    return mirror


def clone_repo(
    repo: str,
    base_commit: str,
    repo_dir: Path,
    repo_cache: Path | None = None,
) -> None:
    if repo_dir.exists() and (repo_dir / ".git").exists():
        print(f"    repo already exists, resetting to {base_commit[:8]}")
        if not _commit_exists(repo_dir, base_commit, bare=False):
            subprocess.run(
                ["git", "-C", str(repo_dir), "fetch", "--quiet", "origin"],
                check=False,
            )
        if not _commit_exists(repo_dir, base_commit, bare=False):
            raise RuntimeError(
                f"existing repository for {repo} does not contain {base_commit}"
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
    clone_args = ["git", "clone", "--quiet"]
    if repo_cache is not None:
        mirror = ensure_repo_mirror(repo, repo_cache, base_commit)
        clone_args.extend(["--reference-if-able", str(mirror)])
        clone_args.extend(["--no-checkout", str(mirror), str(repo_dir)])
    else:
        clone_args.extend([url, str(repo_dir)])
    result = subprocess.run(
        clone_args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"    ERROR cloning: {result.stderr}")
        raise RuntimeError(f"git clone failed for {repo}")

    if repo_cache is not None:
        subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "set-url", "origin", url],
            check=True,
        )
    print(f"    resetting to {base_commit[:8]}")
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", base_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "clean", "-fd"],
        check=True,
    )


def setup_issue(
    label: int,
    instance: dict,
    workdir: Path,
    repo_cache: Path | None = None,
) -> None:
    issue_dir = workdir / f"swe_issue_{label:03d}"
    artifacts_dir = issue_dir / "artifacts"
    repo_dir = issue_dir / "repo"

    artifacts_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = artifacts_dir / "instance_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(instance, f, indent=2, ensure_ascii=False)
    print(f"  saved metadata -> {metadata_path}")

    print(f"  setting up repo for {instance['repo']} @ {instance['base_commit'][:8]}")
    clone_repo(instance["repo"], instance["base_commit"], repo_dir, repo_cache)
    print(f"  done: {issue_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SWE-bench Pro cases")
    parser.add_argument("--start", type=int, default=10, help="Dataset start index (0-based)")
    parser.add_argument("--count", type=int, default=5, help="Number of cases to fetch")
    parser.add_argument(
        "--all", action="store_true", help="Prepare every case from the selected source"
    )
    parser.add_argument("--start-label", type=int, default=11, help="Starting issue label number")
    parser.add_argument("--workdir", type=str, default="workdir", help="Output workdir path")
    parser.add_argument(
        "--source-jsonl",
        type=Path,
        default=None,
        help="Use a local dataset JSONL instead of downloading from Hugging Face",
    )
    parser.add_argument(
        "--repo-cache",
        type=Path,
        default=None,
        help="Shared bare-mirror cache (default: <workdir>/_repo_cache)",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir)
    count = None if args.all else args.count
    instances = load_instances(args.start, count, args.source_jsonl)
    repo_cache = args.repo_cache or (workdir / "_repo_cache")

    for i, instance in enumerate(instances):
        label = args.start_label + i
        print(f"\n[{label:03d}] {instance['instance_id']}")
        setup_issue(label, instance, workdir, repo_cache)

    print("\nAll done.")


if __name__ == "__main__":
    main()
