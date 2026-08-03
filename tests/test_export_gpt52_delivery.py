from __future__ import annotations

import json

from scripts import export_gpt52_delivery as delivery
from scripts.export_gpt52_delivery import classify_attempt, failure_category


def test_failure_category_distinguishes_docker_from_patch_quality(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "runner_task.json").write_text(
        json.dumps({"status": "infra_failed", "failure_kind": "docker_infra"}),
        encoding="utf-8",
    )
    assert failure_category(output) == "infra/docker"

    (output / "runner_task.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    (output / "patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (output / "eval_result").mkdir()
    (output / "eval_result" / "eval_summary.json").write_text(
        json.dumps({"resolved": False}), encoding="utf-8"
    )
    assert failure_category(output) == "patch/unresolved"


def test_attempt_classifier_keeps_api_and_evaluator_failures_distinct():
    assert classify_attempt(
        {"status": "infra_failed", "failure_kind": "model_infra"}
    ) == "infra/api"
    assert classify_attempt(
        {"status": "failed", "phase": "phase3", "error": "eval parser crashed"}
    ) == "harness/evaluator"


def test_missing_delivery_artifacts_requires_patch_eval_and_core_metrics(tmp_path):
    output = tmp_path / "output"
    (output / "eval_result").mkdir(parents=True)
    (output / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
        encoding="utf-8",
    )
    (output / "eval_result" / "eval_summary.json").write_text(
        json.dumps({"resolved": False}), encoding="utf-8"
    )
    for name in delivery.REQUIRED_DELIVERY_ARTIFACTS:
        (output / name).write_text("{}\n", encoding="utf-8")
    (output / "run_metrics.json").write_text("{}\n", encoding="utf-8")

    assert delivery.missing_delivery_artifacts(output) == []

    (output / "run_metrics.json").write_text("", encoding="utf-8")
    assert delivery.missing_delivery_artifacts(output) == ["run_metrics*.json"]


def test_audit_delivery_reports_artifact_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery, "WORKDIR", tmp_path)
    output = tmp_path / "swe_issue_001" / delivery.OUTPUT_SUBDIR
    (output / "eval_result").mkdir(parents=True)
    (output / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
        encoding="utf-8",
    )
    (output / "eval_result" / "eval_summary.json").write_text(
        json.dumps({"resolved": True}), encoding="utf-8"
    )
    for name in delivery.REQUIRED_DELIVERY_ARTIFACTS:
        (output / name).write_text("{}\n", encoding="utf-8")
    (output / "run_metrics.analysis.json").write_text("{}\n", encoding="utf-8")

    audit = delivery.audit_delivery()

    assert audit["expected_cases"] == 731
    assert audit["ready_cases"] == 1
    assert len(audit["incomplete_cases"]) == 730
    assert audit["artifact_coverage"]["patch.diff"] == 1
    assert audit["artifact_coverage"]["eval_result/eval_summary.json"] == 1
    assert audit["artifact_coverage"]["run_metrics*.json"] == 1
