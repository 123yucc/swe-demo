from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.local_swebench_runner import (
    create_container,
    image_candidates,
    pull_first_image,
    start_container_detached,
)
from src.models.patch import PatchPlan
from src.orchestrator.artifact_verify import verify_patch_artifacts
from src.orchestrator.build_verify import (
    changed_go_packages,
    changed_python_production_files,
    detect_build_system,
    run_build_check,
)
from src.orchestrator.consistency_checks import (
    check_config_entry_shape,
    check_contract_drift,
    check_go_unexport_consistency,
    check_parallel_impl_consistency,
    check_removed_symbol_test_refs,
    check_rename_residue,
    check_undefined_config_symbol,
)


ROOT = Path("/home/user/demo")
ISSUE_DIR = ROOT / "workdir/swe_issue_024"
OUTPUT_DIR = ISSUE_DIR / os.environ.get(
    "OUTPUT_SUBDIR", "outputs_clean-knowledge-gpt5.2-r17"
)
REPO_DIR = ISSUE_DIR / "repo"
METADATA_PATH = ISSUE_DIR / "artifacts/instance_metadata.json"
LOG_PATH = OUTPUT_DIR / "logs/stage2_revalidate.log"
MEMORY_GB = 6.0


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _err_to_log(err: Any) -> dict[str, Any]:
    if hasattr(err, "model_dump"):
        return err.model_dump()
    if hasattr(err, "__dict__"):
        return dict(err.__dict__)
    return {"message": str(err)}


def _remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=60,
    )


def main() -> int:
    if not shutil.which("docker"):
        raise SystemExit("docker executable not found")
    patch_path = OUTPUT_DIR / "patch.diff"
    plan_path = OUTPUT_DIR / "patch_plan.json"
    if not patch_path.is_file() or not plan_path.is_file():
        raise SystemExit("patch.diff and patch_plan.json are required")

    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    plan = PatchPlan.model_validate(_load_json(plan_path))

    artifact = verify_patch_artifacts(REPO_DIR, plan, patch_text)
    artifact_log = [
        {
            "round": "revalidate",
            "ok": artifact.ok,
            "empty_patch": artifact.empty_patch,
            "diff_paths": artifact.diff_paths,
            "planned_required_files": artifact.planned_required_files,
            "findings": [f.model_dump() for f in artifact.findings],
        }
    ]
    _write_json(OUTPUT_DIR / "artifact_verification.json", artifact_log)
    if not artifact.ok:
        _write_json(
            OUTPUT_DIR / "patch_outcome.json",
            {
                "issue_id": _load_json(METADATA_PATH).get("instance_id"),
                "closure_checker_approved": False,
                "patch_outcome": "PATCH_INCOMPLETE",
                "revalidated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print("artifact gate failed")
        return 1

    metadata = _load_json(METADATA_PATH)
    image = pull_first_image(
        image_candidates(metadata, ["jefzda"]),
        platform=None,
        log_path=LOG_PATH,
    )
    cname = f"stage2_revalidate_024_{os.getpid()}"
    create_container(
        name=cname,
        image=image,
        command=["-c", "trap 'exit 0' TERM INT; while true; do sleep 3600; done"],
        log_path=LOG_PATH,
        env={"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        volumes=None,
        memory_gb=MEMORY_GB,
        platform=None,
        entrypoint="/bin/bash",
    )
    start_container_detached(cname, LOG_PATH)

    old_container = os.environ.get("REPO_EXECUTOR_DOCKER_CONTAINER")
    old_workdir = os.environ.get("REPO_EXECUTOR_CONTAINER_WORKDIR")
    os.environ["REPO_EXECUTOR_DOCKER_CONTAINER"] = cname
    os.environ["REPO_EXECUTOR_CONTAINER_WORKDIR"] = "/app"
    try:
        system = detect_build_system(REPO_DIR)
        contract_drift = check_contract_drift(REPO_DIR, base_commit=None)
        parallel_impl = check_parallel_impl_consistency(REPO_DIR, base_commit=None)
        removed_sym_refs = check_removed_symbol_test_refs(REPO_DIR, base_commit=None)
        go_unexport = check_go_unexport_consistency(REPO_DIR, base_commit=None)
        config_shape = check_config_entry_shape(REPO_DIR, base_commit=None)
        heuristic_errors = (
            list(contract_drift)
            + list(parallel_impl)
            + list(removed_sym_refs)
            + list(go_unexport)
            + list(config_shape)
        )
        python_targets = changed_python_production_files(REPO_DIR) if system == "python" else None
        go_targets = changed_go_packages(REPO_DIR) if system == "go" else None
        post = run_build_check(
            REPO_DIR,
            system,
            python_targets=python_targets,
            go_targets=go_targets,
        )
        residues = check_rename_residue(REPO_DIR, base_commit=None)
        config_sym_errors = check_undefined_config_symbol(REPO_DIR, base_commit=None)
        static_warnings = list(residues) + list(config_sym_errors) + heuristic_errors

        compile_record: dict[str, Any] = {
            "system": system,
            "command": post.command,
            "python_targets": python_targets or [],
            "go_targets": go_targets or [],
            "timed_out": post.timed_out,
            "revalidated_at": datetime.now(timezone.utc).isoformat(),
            "static_warnings": [_err_to_log(e) for e in static_warnings],
        }
        if static_warnings:
            compile_record["outcome"] = "STATIC_GATE_FAILED"
            patch_outcome = "BUILD_FAILED_NO_REPAIR"
            ok = False
        elif post.unverifiable:
            compile_record["outcome"] = "UNVERIFIABLE"
            compile_record["raw_output_tail"] = (post.raw_output or "")[-2000:]
            patch_outcome = "BUILD_UNVERIFIABLE"
            ok = True
        elif post.ok:
            compile_record["outcome"] = "PASSED"
            patch_outcome = "PATCH_SUCCESS"
            ok = True
        else:
            compile_record["outcome"] = "FAILED_NO_REPAIR"
            compile_record["raw_output_tail"] = (post.raw_output or "")[-4000:]
            compile_record["errors"] = [_err_to_log(e) for e in post.errors]
            patch_outcome = "BUILD_FAILED_NO_REPAIR"
            ok = False

        _write_json(OUTPUT_DIR / "compile_check.json", [compile_record])
        _write_json(
            OUTPUT_DIR / "patch_outcome.json",
            {
                "issue_id": metadata.get("instance_id"),
                "closure_checker_approved": ok,
                "patch_outcome": patch_outcome,
                "revalidated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"artifact_ok=True system={system} build_ok={post.ok} outcome={patch_outcome}")
        if static_warnings:
            print(f"static_warnings={len(static_warnings)}")
        if post.errors:
            print(f"build_errors={len(post.errors)}")
        return 0 if ok else 1
    finally:
        if old_container is None:
            os.environ.pop("REPO_EXECUTOR_DOCKER_CONTAINER", None)
        else:
            os.environ["REPO_EXECUTOR_DOCKER_CONTAINER"] = old_container
        if old_workdir is None:
            os.environ.pop("REPO_EXECUTOR_CONTAINER_WORKDIR", None)
        else:
            os.environ["REPO_EXECUTOR_CONTAINER_WORKDIR"] = old_workdir
        _remove_container(cname)


if __name__ == "__main__":
    sys.exit(main())
