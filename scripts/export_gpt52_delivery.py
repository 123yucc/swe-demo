#!/usr/bin/env python3
"""Export patch, official eval, metrics, provenance, and checksums for GPT-5.2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
RUNTIME = ROOT / "runtime" / "gpt52-731"
DEFAULT_ROOT = ROOT / "deliverables" / "gpt-5.2"
OUTPUT_SUBDIR = "outputs_gpt-5.2"
METRIC_ARTIFACTS = (
    "patch_outcome.json",
    "prediction.json",
    "runner_task.json",
    "analysis_stage.json",
    "run_metrics.analysis.json",
    "run_metrics.json",
    "model_calls.jsonl",
    "working_memory.json",
    "evidence.json",
    "compile_check.json",
    "build_verification.json",
    "dynamic_closure.json",
)
REQUIRED_DELIVERY_ARTIFACTS = (
    "patch_outcome.json",
    "prediction.json",
    "runner_task.json",
)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def effective_patch(path: Path) -> bool:
    try:
        return any(
            line.startswith("diff --git ")
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False


def nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def missing_delivery_artifacts(output: Path) -> list[str]:
    missing = []
    if not effective_patch(output / "patch.diff"):
        missing.append("patch.diff")
    summary = load_json(output / "eval_result" / "eval_summary.json")
    if summary.get("resolved") not in {True, False}:
        missing.append("eval_result/eval_summary.json")
    for name in REQUIRED_DELIVERY_ARTIFACTS:
        if not nonempty_file(output / name):
            missing.append(name)
    if not any(
        nonempty_file(output / name)
        for name in ("run_metrics.analysis.json", "run_metrics.json")
    ):
        missing.append("run_metrics*.json")
    return missing


def audit_delivery() -> dict:
    cases = []
    coverage_names = (
        "patch.diff",
        "eval_result/eval_summary.json",
        "run_metrics*.json",
        *METRIC_ARTIFACTS,
    )
    coverage = Counter({name: 0 for name in coverage_names})
    for label in range(1, 732):
        issue = f"swe_issue_{label:03d}"
        output = WORKDIR / issue / OUTPUT_SUBDIR
        missing = missing_delivery_artifacts(output)
        summary = load_json(output / "eval_result" / "eval_summary.json")
        row = {
            "issue": issue,
            "ready": not missing,
            "resolved": summary.get("resolved"),
            "failure_category": failure_category(output),
            "runner_status": load_json(output / "runner_task.json").get("status"),
            "patch_outcome": load_json(output / "patch_outcome.json").get("patch_outcome"),
            "missing_artifacts": missing,
        }
        cases.append(row)
        for name in coverage_names:
            path = output / name
            present = (
                effective_patch(path)
                if name == "patch.diff"
                else (
                    load_json(path).get("resolved") in {True, False}
                    if name == "eval_result/eval_summary.json"
                    else (
                        any(
                            nonempty_file(output / metric_name)
                            for metric_name in (
                                "run_metrics.analysis.json",
                                "run_metrics.json",
                            )
                        )
                        if name == "run_metrics*.json"
                        else nonempty_file(path)
                    )
                )
            )
            coverage[name] += int(present)
    incomplete = [row["issue"] for row in cases if not row["ready"]]
    return {
        "expected_cases": 731,
        "ready_cases": 731 - len(incomplete),
        "incomplete_cases": incomplete,
        "artifact_coverage": dict(coverage),
        "cases": cases,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def failure_category(output: Path) -> str:
    task = load_json(output / "runner_task.json")
    outcome = str(load_json(output / "patch_outcome.json").get("patch_outcome") or "")
    patch = output / "patch.diff"
    summary = load_json(output / "eval_result" / "eval_summary.json")
    if task.get("status") == "infra_failed":
        return {
            "docker_infra": "infra/docker",
            "model_infra": "infra/api",
        }.get(str(task.get("failure_kind") or ""), "infra/unknown")
    if task.get("status") == "failed":
        phase = str(task.get("phase") or "")
        error = str(task.get("error") or "").lower()
        if "rate limit" in error or "429" in error:
            return "infra/api_rate_limit"
        if phase == "analysis":
            return "analysis/failure"
        if phase in {"generate", "stage2"}:
            return "patch/generation_or_gate"
        if phase in {"evaluate", "phase3"} or "eval" in error:
            return "harness/evaluator"
    if not patch.is_file():
        return "patch/missing"
    if not effective_patch(patch):
        return "patch/empty_or_invalid"
    if not summary:
        if "BUILD_FAILED" in outcome or outcome in {"PATCH_INCOMPLETE", "STATIC_GATE_FAILED"}:
            return "patch/build_or_static_gate"
        if task.get("status") == "failed":
            error = str(task.get("error") or "").lower()
            if "eval" in error or "evaluator" in error:
                return "harness/evaluator"
            return "harness/task"
        return "eval/missing"
    if summary.get("resolved") is False:
        return "patch/unresolved"
    return ""


def classify_attempt(payload: dict) -> str:
    status = str(payload.get("status") or "")
    failure_kind = str(payload.get("failure_kind") or "")
    phase = str(payload.get("phase") or "")
    error = str(payload.get("error") or "").lower()
    if status == "success":
        return ""
    if status == "infra_failed":
        return {
            "docker_infra": "infra/docker",
            "model_infra": "infra/api",
        }.get(failure_kind, "infra/unknown")
    if status != "failed":
        return status or "unknown"
    if "rate limit" in error or "429" in error:
        return "infra/api_rate_limit"
    if "repository" in error or "git clone" in error:
        return "repo/preparation"
    if phase == "analysis" or "evidence" in error or "closure" in error:
        return "analysis/failure"
    if phase in {"generate", "stage2"} or "patch" in error or "compile" in error:
        return "patch/generation_or_gate"
    if phase in {"evaluate", "phase3"} or "eval" in error:
        return "harness/evaluator"
    return "harness/internal"


def collect_attempt_history() -> list[dict]:
    rows = []
    for state_path in sorted((ROOT / "logs" / "runs").glob("gpt52*/runner.state.json")):
        tasks = load_json(state_path).get("tasks") or {}
        for key, payload in tasks.items():
            if not isinstance(payload, dict):
                continue
            rows.append(
                {
                    "run_name": state_path.parent.name,
                    "task_key": key,
                    "issue": payload.get("issue"),
                    "status": payload.get("status"),
                    "retryable": payload.get("retryable"),
                    "failure_category": classify_attempt(payload),
                    "failure_kind": payload.get("failure_kind"),
                    "phase": payload.get("phase"),
                    "patch_outcome": payload.get("patch_outcome"),
                    "resolved": payload.get("resolved"),
                    "error": payload.get("error"),
                    "run_id": payload.get("run_id"),
                    "started_at": payload.get("started_at"),
                    "finished_at": payload.get("finished_at"),
                }
            )
    return rows


def export(destination: Path, *, allow_incomplete: bool = False) -> dict:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite delivery: {destination}")
    audit = audit_delivery()
    cases = audit["cases"]
    incomplete = audit["incomplete_cases"]
    if incomplete and not allow_incomplete:
        raise RuntimeError(
            f"delivery is incomplete: {len(incomplete)} cases lack an effective patch/eval; "
            f"first={incomplete[:10]}"
        )

    destination.mkdir(parents=True)
    for row in cases:
        issue = row["issue"]
        issue_dir = WORKDIR / issue
        output = issue_dir / OUTPUT_SUBDIR
        target = destination / "cases" / issue
        target.mkdir(parents=True)
        metadata = issue_dir / "artifacts" / "instance_metadata.json"
        sources = [metadata, output / "patch.diff", *(output / name for name in METRIC_ARTIFACTS)]
        for source in sources:
            if not source.is_file():
                continue
            target_path = target / (
                "instance_metadata.json" if source == metadata else source.name
            )
            shutil.copy2(source, target_path)
        eval_source = output / "eval_result"
        if eval_source.is_dir():
            shutil.copytree(eval_source, target / "eval_result")

    metrics_dir = destination / "metrics"
    subprocess.run(
        [
            "python3",
            "scripts/collect_metrics.py",
            "--output-subdir",
            OUTPUT_SUBDIR,
            "--first-n",
            "731",
            "--output-dir",
            str(metrics_dir),
        ],
        cwd=ROOT,
        check=True,
    )
    with (destination / "failure_diagnosis.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cases[0]))
        writer.writeheader()
        writer.writerows(cases)

    attempt_rows = collect_attempt_history()
    if attempt_rows:
        with (destination / "failure_attempts.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(attempt_rows[0]))
            writer.writeheader()
            writer.writerows(attempt_rows)
        (destination / "failure_attempts.json").write_text(
            json.dumps(attempt_rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    provenance = destination / "provenance"
    for run_dir in sorted((ROOT / "logs" / "runs").glob("gpt52*")):
        if not run_dir.is_dir():
            continue
        target = provenance / "logs" / "runs" / run_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for name in ("runner.state.json", "runner.status", "runner.log"):
            source = run_dir / name
            if source.is_file():
                shutil.copy2(source, target / name)
    runtime_target = provenance / "runtime" / "gpt52-731"
    runtime_target.mkdir(parents=True, exist_ok=True)
    for name in ("supervisor.state.json", "supervisor.status", "supervisor.log"):
        source = RUNTIME / name
        if source.is_file():
            shutil.copy2(source, runtime_target / name)
    for name in ("history", "manifests", "eval-retry"):
        source = RUNTIME / name
        if source.is_dir():
            shutil.copytree(source, runtime_target / name)

    checksums = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "delivery_manifest.json":
            checksums.append(
                {
                    "path": path.relative_to(destination).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": "gpt-5.2",
        "expected_cases": 731,
        "ready_cases": 731 - len(incomplete),
        "incomplete_cases": incomplete,
        "attempt_records": len(attempt_rows),
        "artifacts": checksums,
    }
    (destination / "delivery_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if args.audit_only:
        audit = audit_delivery()
        incomplete_rows = [row for row in audit["cases"] if not row["ready"]]
        print(
            json.dumps(
                {
                    "expected_cases": audit["expected_cases"],
                    "ready_cases": audit["ready_cases"],
                    "incomplete_cases": len(audit["incomplete_cases"]),
                    "first_incomplete": audit["incomplete_cases"][:20],
                    "first_incomplete_details": [
                        {
                            "issue": row["issue"],
                            "missing_artifacts": row["missing_artifacts"],
                            "failure_category": row["failure_category"],
                        }
                        for row in incomplete_rows[:20]
                    ],
                    "artifact_coverage": audit["artifact_coverage"],
                },
                ensure_ascii=False,
            )
        )
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (args.destination or DEFAULT_ROOT / timestamp).resolve()
    manifest = export(destination, allow_incomplete=args.allow_incomplete)
    print(
        json.dumps(
            {
                "destination": str(destination),
                "ready_cases": manifest["ready_cases"],
                "incomplete_cases": len(manifest["incomplete_cases"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
