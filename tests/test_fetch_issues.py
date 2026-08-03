from __future__ import annotations

import json
import subprocess

from scripts.fetch_issues import clone_repo, load_instances, _mirror_path


def test_load_all_instances_from_local_jsonl(tmp_path):
    source = tmp_path / "cases.jsonl"
    source.write_text("\n".join(json.dumps({"id": n}) for n in range(3)), encoding="utf-8")
    assert [row["id"] for row in load_instances(0, None, source)] == [0, 1, 2]
    assert [row["id"] for row in load_instances(1, 1, source)] == [1]


def test_mirror_path_is_repo_specific(tmp_path):
    assert _mirror_path("owner/repo", tmp_path).name == "owner--repo.git"


def test_clone_repo_uses_cached_commit_without_remote_access(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Test User"],
        check=True,
    )
    (source / "README.md").write_text("cached\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "cached"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    cache = tmp_path / "cache"
    cache.mkdir()
    mirror = _mirror_path("owner/repo", cache)
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(mirror)], check=True)

    destination = tmp_path / "workdir" / "repo"
    clone_repo("owner/repo", commit, destination, cache)

    assert (destination / "README.md").read_text(encoding="utf-8") == "cached\n"
    assert subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == commit
    assert subprocess.run(
        ["git", "-C", str(destination), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "https://github.com/owner/repo.git"
