"""Run GPT-5.2 cases 081-731 as resumable, disk-bounded staged batches."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.fetch_issues import setup_issue
except ModuleNotFoundError:  # Direct execution adds scripts/, not its parent, to sys.path.
    from fetch_issues import setup_issue

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
RUNTIME = ROOT / "runtime" / "gpt52-731"
SOURCE_MANIFEST = ROOT / "eval" / "manifests" / "swebench-pro-081-731.gpt5.2.json"
STATE_PATH = RUNTIME / "supervisor.state.json"
LOCK_PATH = RUNTIME / "supervisor.lock"
REPO_CACHE = WORKDIR / "_repo_cache"
KNOWN_HEAVY = {
    81, 82, 83, 84, 85, 86, 87, 90, 92, 94, 95, 96, 98, 99, 100,
}
DELIVERABLE_PATCH_OUTCOMES = {
    "PATCH_SUCCESS",
    "BUILD_UNVERIFIABLE",
    "BUILD_FAILED",
    "BUILD_FAILED_NO_REPAIR",
    "BUILD_FAILED_AFTER_REPAIR",
    "PATCH_FAILED",
    "PATCH_INCOMPLETE",
    "PARTIAL_PATCH",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def output_dir(label: int, subdir: str) -> Path:
    return WORKDIR / f"swe_issue_{label:03d}" / subdir


def effective_patch(path: Path) -> bool:
    try:
        return any(line.startswith("diff --git ") for line in path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines())
    except OSError:
        return False


def phase3_complete(label: int, subdir: str) -> bool:
    out = output_dir(label, subdir)
    required = (
        out / "compile_check.json",
        out / "dynamic_closure.json",
        out / "eval_result" / "eval_summary.json",
    )
    if not effective_patch(out / "patch.diff") or not all(path.is_file() for path in required):
        return False
    try:
        outcome = json.loads((out / "patch_outcome.json").read_text()).get("patch_outcome")
    except (OSError, json.JSONDecodeError):
        return False
    return outcome in DELIVERABLE_PATCH_OUTCOMES


def latest_attempt(label: int, subdir: str) -> dict:
    path = output_dir(label, subdir) / "runner_attempt.latest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def disk_free_gib() -> float:
    return shutil.disk_usage(ROOT).free / 1024**3


def disk_floor_gib(env_name: str, default: float) -> float:
    """Read an explicit disk safety floor while keeping conservative defaults."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid {env_name}={raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"invalid negative {env_name}={raw!r}")
    return value


def live_runner_pids() -> list[int]:
    pids: list[int] = []
    for path in (ROOT / "logs" / "runs").glob("gpt52-731-*/runner.pid"):
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)
            pids.append(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return sorted(set(pids))


def wait_for_orphan_runners(state: dict) -> None:
    while True:
        pids = live_runner_pids()
        if not pids:
            state.pop("orphan_runner_pids", None)
            return
        state.update({
            "status": "waiting_for_orphan_runner",
            "orphan_runner_pids": pids,
            "updated_at": utc_now(),
        })
        write_json(STATE_PATH, state)
        print(f"waiting for prior detached runner(s): {pids}", flush=True)
        time.sleep(60)


def repo_size_gib(label: int) -> float:
    repo = WORKDIR / f"swe_issue_{label:03d}" / "repo"
    result = subprocess.run(
        ["du", "-s", "--block-size=1", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return int(result.stdout.split()[0]) / 1024**3
    except (IndexError, ValueError):
        return 0.0


def prepare(
    labels: list[int], dataset: list[dict] | None
) -> tuple[list[dict] | None, list[int], dict[int, str]]:
    before_floor = disk_floor_gib("GPT52_MIN_FREE_BEFORE_GIB", 80.0)
    after_floor = disk_floor_gib("GPT52_MIN_FREE_AFTER_GIB", 60.0)
    if disk_free_gib() < before_floor:
        raise RuntimeError(
            f"less than {before_floor:g} GiB free before repository preparation"
        )
    prepared: list[int] = []
    failures: dict[int, str] = {}
    for label in labels:
        metadata = WORKDIR / f"swe_issue_{label:03d}" / "artifacts" / "instance_metadata.json"
        if metadata.is_file():
            instance = json.loads(metadata.read_text(encoding="utf-8"))
        else:
            if dataset is None:
                from datasets import load_dataset

                dataset = [dict(row) for row in load_dataset(
                    "ScaleAI/SWE-bench_Pro", split="test"
                )]
                if len(dataset) != 731:
                    raise RuntimeError(f"expected 731 dataset rows, found {len(dataset)}")
            instance = dataset[label - 1]
        for attempt in range(1, 4):
            try:
                setup_issue(label, instance, WORKDIR, REPO_CACHE)
                repo = WORKDIR / f"swe_issue_{label:03d}" / "repo" / ".git"
                if not repo.exists():
                    raise RuntimeError("prepared repository has no .git")
                prepared.append(label)
                failures.pop(label, None)
                break
            except Exception as exc:
                failures[label] = repr(exc)
                print(
                    f"[prepare] issue={label:03d} attempt={attempt}/3 failed: {exc}",
                    flush=True,
                )
                partial = WORKDIR / f"swe_issue_{label:03d}" / "repo"
                if partial.exists() and not (partial / ".git").exists():
                    shutil.rmtree(partial)
                if attempt < 3:
                    time.sleep(5 * attempt)
    if disk_free_gib() < after_floor:
        raise RuntimeError(
            f"less than {after_floor:g} GiB free after repository preparation"
        )
    return dataset, prepared, failures


def cleanup_repositories(labels: list[int]) -> None:
    workdir_root = WORKDIR.resolve()
    for label in labels:
        repo = (WORKDIR / f"swe_issue_{label:03d}" / "repo").resolve()
        if repo.parent.parent != workdir_root or not 81 <= label <= 731:
            raise RuntimeError(f"refusing unsafe repository cleanup: {repo}")
        if repo.exists():
            shutil.rmtree(repo)


def cleanup_new_repos_enabled() -> bool:
    return (
        os.environ.get("GPT52_CLEANUP_NEW_REPOS") == "1"
        and os.environ.get("GPT52_PRESERVE_NEW_REPOS") != "1"
    )


def allow_failed_patch_eval(pass_number: int, max_passes: int) -> bool:
    """Freeze/evaluate failed-build patches only after repair passes are exhausted."""
    return pass_number >= max_passes


def retry_scope_from_state(
    previous_state: dict, first: int, last: int
) -> set[int] | None:
    """Limit endpoint recovery to cases whose latest attempt was infrastructure-failed."""
    if previous_state.get("status") != "waiting_for_model":
        return None
    labels: set[int] = set()
    for value in previous_state.get("infra_remaining_issues") or []:
        try:
            label = int(value)
        except (TypeError, ValueError):
            continue
        if first <= label <= last:
            labels.add(label)
    return labels


def batch_manifest(
    source: dict,
    labels: list[int],
    start: int,
    end: int,
    attempt_id: str | None = None,
) -> Path:
    document = json.loads(json.dumps(source))
    document.pop("issue_range", None)
    document.pop("batch_size", None)
    document["issues"] = [f"{label:03d}" for label in labels]
    document["expected_issue_count"] = len(labels)
    manifest_root = RUNTIME / "manifests"
    if attempt_id:
        manifest_root /= attempt_id
    path = manifest_root / f"batch-{start:03d}-{end:03d}.json"
    write_json(path, document)
    return path


def main() -> int:
    import fcntl

    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another gpt52-731 supervisor is already running", flush=True)
        return 2

    source = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    issue_range = source["issue_range"]
    first, last = int(issue_range["start"]), int(issue_range["end"])
    batch_size = int(os.environ.get("GPT52_BATCH_SIZE", source.get("batch_size", 40)))
    subdir = source["models"][0]["output_subdir"]
    dataset: list[dict] | None = None
    pass_number = int(os.environ.get("GPT52_RECOVERY_PASS", "1"))
    max_passes = int(os.environ.get("GPT52_MAX_RECOVERY_PASSES", "3"))
    attempt_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-"
        f"p{pass_number:02d}-{os.getpid()}"
    )
    previous_state: dict = {}
    if STATE_PATH.is_file():
        try:
            loaded_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded_state, dict):
                previous_state = loaded_state
        except (OSError, json.JSONDecodeError):
            previous_state = {}
        history_path = RUNTIME / "history" / f"supervisor.{attempt_id}.previous-state.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(STATE_PATH, history_path)
    retry_scope = retry_scope_from_state(previous_state, first, last)
    state = {
        "status": "running",
        "attempt_id": attempt_id,
        "recovery_pass": pass_number,
        "max_recovery_passes": max_passes,
        "pid": os.getpid(),
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "range": [first, last],
        "batch_size": batch_size,
        "output_subdir": subdir,
        "retry_scope": (
            [f"{label:03d}" for label in sorted(retry_scope)]
            if retry_scope is not None
            else "all_incomplete"
        ),
        "batches": [],
    }
    write_json(STATE_PATH, state)
    wait_for_orphan_runners(state)
    state["status"] = "running"
    state["updated_at"] = utc_now()
    write_json(STATE_PATH, state)

    preparation_failed_labels: set[int] = set()
    for start in range(first, last + 1, batch_size):
        end = min(last, start + batch_size - 1)
        labels = [
            label
            for label in range(start, end + 1)
            if not phase3_complete(label, subdir)
            and (retry_scope is None or label in retry_scope)
        ]
        batch = {
            "range": [start, end],
            "pending_at_start": len(labels),
            "status": "skipped_complete" if not labels else "preparing",
            "started_at": utc_now(),
        }
        state["current_batch"] = [start, end]
        state["batches"].append(batch)
        state["updated_at"] = utc_now()
        write_json(STATE_PATH, state)
        if not labels:
            continue

        repos_present_before = {
            label
            for label in labels
            if (WORKDIR / f"swe_issue_{label:03d}" / "repo").exists()
        }
        prepared: list[int] = []
        try:
            dataset, prepared, preparation_failures = prepare(labels, dataset)
            preparation_failed_labels.update(preparation_failures)
            if not prepared:
                raise RuntimeError(
                    f"no repositories prepared; failures={preparation_failures}"
                )
            heavy = sorted(
                label for label in prepared
                if label in KNOWN_HEAVY or repo_size_gib(label) >= 2.0
            )
            manifest = batch_manifest(source, prepared, start, end, attempt_id)
            batch.update({
                "status": "running_stages",
                "prepared_count": len(prepared),
                "preparation_failures": {
                    f"{label:03d}": error
                    for label, error in preparation_failures.items()
                },
                "heavy_issues": heavy,
                "manifest": str(manifest),
                "disk_free_gib_after_prepare": round(disk_free_gib(), 2),
            })
            state["updated_at"] = utc_now()
            write_json(STATE_PATH, state)
            env = os.environ.copy()
            env["HEAVY_ISSUES"] = ",".join(f"{label:03d}" for label in heavy)
            # One staged attempt per supervisor pass gives every incomplete case
            # at most max_recovery_passes autonomous attempts in total.
            env["STAGED_INFRA_MAX_ATTEMPTS"] = "1"
            # Only after all patch-repair passes are exhausted do we freeze and
            # officially evaluate the best effective failed-build patch. This
            # guarantees complete eval coverage without wasting earlier passes.
            env["ALLOW_FAILED_PATCH_EVAL"] = (
                "1" if allow_failed_patch_eval(pass_number, max_passes) else "0"
            )
            run_name = f"gpt52-731-{attempt_id}-b{start:03d}-{end:03d}"
            completed = subprocess.run(
                ["bash", "scripts/remote_run_staged_batch.sh", str(manifest), run_name],
                cwd=ROOT,
                env=env,
                check=False,
            )
            remaining = sum(not phase3_complete(label, subdir) for label in labels)
            batch.update({
                "status": "complete" if remaining == 0 else "incomplete",
                "runner_exit_code": completed.returncode,
                "remaining": remaining,
                "finished_at": utc_now(),
            })
        except Exception as exc:
            batch.update({"status": "failed", "error": repr(exc), "finished_at": utc_now()})
        finally:
            transient_repos = [
                label for label in labels if label not in repos_present_before
            ]
            if cleanup_new_repos_enabled():
                cleanup_repositories(transient_repos)
                batch["repository_cleanup"] = {
                    "policy": "new_transient_only",
                    "removed": [f"{label:03d}" for label in transient_repos],
                    "protected_preexisting": [
                        f"{label:03d}" for label in sorted(repos_present_before)
                    ],
                }
            else:
                batch["repository_cleanup"] = "disabled_preserve_all_repos"
            batch["disk_free_gib_after_batch"] = round(disk_free_gib(), 2)
            state["updated_at"] = utc_now()
            write_json(STATE_PATH, state)

    remaining_labels = [
        label for label in range(first, last + 1) if not phase3_complete(label, subdir)
    ]
    state.pop("current_batch", None)
    infra_remaining = [
        label
        for label in remaining_labels
        if latest_attempt(label, subdir).get("status") == "infra_failed"
        or label in preparation_failed_labels
    ]
    terminal_status = (
        "complete"
        if not remaining_labels
        else "waiting_for_model"
        if infra_remaining
        else "needs_manual_recovery"
    )
    state.update({
        "status": terminal_status,
        "remaining_count": len(remaining_labels),
        "remaining_issues": [f"{label:03d}" for label in remaining_labels],
        "infra_remaining_issues": [f"{label:03d}" for label in infra_remaining],
        "finished_at": utc_now(),
        "updated_at": utc_now(),
    })
    write_json(STATE_PATH, state)
    # A pass is never recursively re-executed. Infrastructure recovery is
    # delegated to the endpoint-gated watcher; semantic failures require the
    # bounded targeted path and must not trigger a blind full-analysis rerun.
    if not remaining_labels:
        return 0
    return 75 if infra_remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
