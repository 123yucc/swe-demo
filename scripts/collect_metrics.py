"""
Collect experiment metrics from all swe_issue_* workdirs.

Produces two CSV files:
  workdir/eval_result/metrics_summary.csv   — aggregate stats per case
  workdir/eval_result/metrics_detail.csv    — one row per case with all fields

Usage:
    python scripts/collect_metrics.py [--eval-results workdir/eval_result/eval_results.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.output_paths import model_output_dir_name


WORKDIR = REPO_ROOT / "workdir"
EVAL_RESULT_DIR = WORKDIR / "eval_result"


def load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_model_calls(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def count_action_events(action_history: list[dict], **filters) -> int:
    """Count events in action_history matching all provided filters."""
    count = 0
    for evt in action_history:
        if all(str(evt.get(k, "")).startswith(str(v)) for k, v in filters.items()):
            count += 1
    return count


def get_deep_search_rounds(action_history: list[dict]) -> int:
    return count_action_events(action_history, phase="deep-search")


def get_rework_rounds(action_history: list[dict]) -> int:
    return count_action_events(action_history, outcome="rework")


def get_llm_invocations(action_history: list[dict]) -> int:
    return sum(1 for evt in action_history if evt.get("subagent", ""))


def get_closure_forced_fail(action_history: list[dict]) -> bool:
    return any(
        "CLOSURE_FORCED_FAIL" in str(evt.get("outcome", "")) or
        "EVIDENCE_INCOMPLETE" in str(evt.get("outcome", ""))
        for evt in action_history
    )


def get_verdict_distribution(requirements: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {
        "AS_IS_VIOLATED": 0,
        "AS_IS_COMPLIANT": 0,
        "TO_BE_MISSING": 0,
        "TO_BE_PARTIAL": 0,
        "UNCHECKED": 0,
    }
    for req in requirements:
        verdict = req.get("verdict", "UNCHECKED")
        dist[verdict] = dist.get(verdict, 0) + 1
    return dist


def count_evidence_items(evidence_cards: dict) -> dict[str, int]:
    """Count items in each evidence card field."""
    counts: dict[str, int] = {}
    for card_name in ("symptom", "constraint", "localization", "structural"):
        card = evidence_cards.get(card_name, {})
        for field, val in card.items():
            if isinstance(val, list):
                counts[f"{card_name}.{field}"] = len(val)
    counts["requirements_total"] = len(evidence_cards.get("requirements", []))
    return counts


def get_build_gate_info(build_verification: list | dict | None) -> dict:
    if not build_verification:
        return {
            "compile_outcome": "NO_COMPILE_LOG",
            "build_system": "unknown",
        }
    # build_verification.json is a list of round dicts
    if isinstance(build_verification, list):
        rounds = build_verification
    else:
        rounds = build_verification.get("rounds", [])
    build_system = rounds[0].get("system", "unknown") if rounds else "unknown"
    last = rounds[-1] if rounds else {}
    outcome = str(last.get("outcome") or "LEGACY_UNKNOWN")
    return {
        "compile_outcome": outcome,
        "build_system": build_system,
    }


def load_per_case_eval_summary(outputs: Path) -> dict:
    summary = load_json(outputs / "eval_result" / "eval_summary.json")
    return summary if isinstance(summary, dict) else {}


def load_runner_task(outputs: Path) -> dict:
    task = load_json(outputs / "runner_task.json")
    return task if isinstance(task, dict) else {}


def load_analysis_stage(outputs: Path) -> dict:
    stage = load_json(outputs / "analysis_stage.json")
    return stage if isinstance(stage, dict) else {}


def is_model_infra_failure(patch_outcome: str, runner_task: dict) -> bool:
    return (
        patch_outcome == "MODEL_INFRA_FAILURE"
        or runner_task.get("failure_kind") == "model_infra"
        or (
            runner_task.get("status") == "infra_failed"
            and not runner_task.get("failure_kind")
            and patch_outcome == "MODEL_INFRA_FAILURE"
        )
    )


def is_docker_infra_failure(patch_outcome: str, runner_task: dict) -> bool:
    return (
        patch_outcome == "DOCKER_INFRA_FAILURE"
        or runner_task.get("failure_kind") == "docker_infra"
    )


def _sum_metric_dicts(parts: list[dict]) -> dict:
    merged: dict = {}
    if not parts:
        return merged
    preferred = parts[-1]
    for key, value in preferred.items():
        merged[key] = value
    for key in (
        "total_cost_usd",
        "estimated_cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "wall_clock_seconds",
    ):
        merged[key] = sum(float(p.get(key, 0) or 0) for p in parts)
    return merged


def load_run_metrics(outputs: Path) -> tuple[dict, str, dict, dict]:
    analysis = load_json(outputs / "run_metrics.analysis.json")
    generate = load_json(outputs / "run_metrics.json")
    analysis = analysis if isinstance(analysis, dict) else {}
    generate = generate if isinstance(generate, dict) else {}

    parts = [p for p in (analysis, generate) if p]
    if not parts:
        return {}, "", {}, {}
    if analysis and generate:
        return _sum_metric_dicts(parts), "run_metrics.analysis.json+run_metrics.json", analysis, generate
    if generate:
        return generate, "run_metrics.json", analysis, generate
    return analysis, "run_metrics.analysis.json", analysis, generate


def failure_reason_for(
    outputs: Path,
    resolved: bool | None,
    patch_outcome: str,
    runner_task: dict,
    analysis_stage: dict,
) -> str:
    if is_model_infra_failure(patch_outcome, runner_task):
        return f"model_infra_failure; patch_outcome={patch_outcome}"
    if is_docker_infra_failure(patch_outcome, runner_task):
        return f"docker_infra_failure; patch_outcome={patch_outcome}"
    if analysis_stage.get("status") == "analysis_complete":
        return ""
    status = runner_task.get("status")
    if status == "failed":
        return str(runner_task.get("error") or "runner_task failed")
    if status == "skipped" and resolved is None:
        return "skipped: " + str(runner_task.get("reason") or "unknown")
    if not outputs.exists():
        return "missing output directory"
    if not (outputs / "patch.diff").exists():
        return "missing patch.diff"
    if resolved is None:
        return "missing eval_summary"
    if resolved is False:
        return f"unresolved; patch_outcome={patch_outcome}"
    return ""


def collect_case(issue_dir: Path, eval_results: dict | None, output_subdir: str) -> dict | None:
    """Collect all metrics for one case. Returns None if no metadata found."""
    meta_path = issue_dir / "artifacts" / "instance_metadata.json"
    if not meta_path.exists():
        return None

    meta = load_json(meta_path) or {}
    instance_id = meta.get("instance_id", issue_dir.name)
    language = meta.get("repo_language", "unknown")
    repo = meta.get("repo", "unknown")

    outputs = issue_dir / output_subdir

    # Patch outcome
    patch_outcome_data = load_json(outputs / "patch_outcome.json") or {}
    patch_outcome = patch_outcome_data.get("patch_outcome", "N/A")
    patch_result = patch_outcome_data.get("patch_result", patch_outcome)
    closure_approved = patch_outcome_data.get("closure_checker_approved", None)

    # Working memory (action_history + evidence)
    wm = load_json(outputs / "working_memory.json") or {}
    action_history = wm.get("action_history", [])
    if not isinstance(action_history, list):
        action_history = []

    # Evidence cards
    evidence_cards = load_json(outputs / "evidence.json") or {}
    requirements = evidence_cards.get("requirements", [])
    verdict_dist = get_verdict_distribution(requirements)

    # Run metrics (timing + cost, only available for cases run with new code)
    run_metrics, metrics_file, analysis_metrics, generate_metrics = load_run_metrics(outputs)
    model_calls = load_model_calls(outputs / "model_calls.jsonl")
    actual_calls = [row for row in model_calls if "wall_clock_ms" in row]
    analysis_stage = load_analysis_stage(outputs)

    # Build verification
    compile_log = load_json(outputs / "compile_check.json")
    if compile_log is None:
        compile_log = load_json(outputs / "build_verification.json")
    build_info = get_build_gate_info(compile_log)
    dynamic = load_json(outputs / "dynamic_closure.json") or {}
    dynamic_counts = dynamic.get("counts", {}) if isinstance(dynamic, dict) else {}

    # Eval result (pass/fail)
    resolved = None
    if eval_results:
        resolved = eval_results.get(instance_id)
    eval_summary = load_per_case_eval_summary(outputs)
    if resolved is None and "resolved" in eval_summary:
        resolved = eval_summary.get("resolved")
    expected_tests = set(eval_summary.get("expected_tests") or [])
    passed_tests = set(eval_summary.get("passed_tests") or [])
    passed_expected = expected_tests & passed_tests
    missing_expected = expected_tests - passed_tests
    runner_task = load_runner_task(outputs)
    runner_status_raw = runner_task.get("status")
    runner_status_effective = (
        runner_status_raw
        if runner_status_raw == "infra_failed"
        else "success"
        if analysis_stage.get("status") == "analysis_complete"
        else runner_status_raw
    )
    model_infra_failure = is_model_infra_failure(patch_outcome, runner_task)
    docker_infra_failure = is_docker_infra_failure(patch_outcome, runner_task)
    infra_failure = model_infra_failure or docker_infra_failure

    # Gold patch complexity
    gold_patch = meta.get("patch", "")
    gold_files = set()
    for line in gold_patch.splitlines():
        if line.startswith("diff --git a/"):
            parts = line.split(" ")
            if len(parts) >= 3:
                gold_files.add(parts[2].lstrip("a/"))
    gold_files_count = len(gold_files)

    row = {
        "issue_dir": issue_dir.name,
        "output_subdir": output_subdir,
        "instance_id": instance_id,
        "repo": repo,
        "language": language,
        "resolved": resolved,
        "swebench_resolved": resolved,
        "patch_result": patch_result,
        "patch_outcome": patch_outcome,
        "harness_patch_outcome": patch_outcome,
        "closure_approved": closure_approved,
        "expected_tests_count": len(expected_tests),
        "passed_expected_tests_count": len(passed_expected),
        "missing_expected_tests_count": len(missing_expected),
        "passed_tests_count": len(passed_tests),
        "has_patch": (outputs / "patch.diff").exists(),
        "has_evidence": (outputs / "evidence.json").exists(),
        "has_prediction": (outputs / "prediction.json").exists(),
        "has_eval_summary": bool(eval_summary),
        "has_analysis_stage": bool(analysis_stage),
        # Process data
        "requirements_total": len(requirements),
        "verdict_AS_IS_VIOLATED": verdict_dist.get("AS_IS_VIOLATED", 0),
        "verdict_AS_IS_COMPLIANT": verdict_dist.get("AS_IS_COMPLIANT", 0),
        "verdict_TO_BE_MISSING": verdict_dist.get("TO_BE_MISSING", 0),
        "verdict_TO_BE_PARTIAL": verdict_dist.get("TO_BE_PARTIAL", 0),
        "verdict_UNCHECKED": verdict_dist.get("UNCHECKED", 0),
        "deep_search_rounds": get_deep_search_rounds(action_history),
        "rework_rounds": get_rework_rounds(action_history),
        "llm_invocations": get_llm_invocations(action_history),
        "closure_forced_fail": get_closure_forced_fail(action_history),
        **build_info,
        "dynamic_closure_passed": dynamic_counts.get("PASS", 0),
        "dynamic_closure_failed": dynamic_counts.get("FAIL", 0),
        "dynamic_closure_unverifiable": (
            dynamic_counts.get("UNVERIFIABLE", 0)
            + dynamic_counts.get("FLAKY_UNVERIFIABLE", 0)
        ),
        "dynamic_closure_seconds": dynamic.get("wall_clock_seconds"),
        "dynamic_closure_input_tokens": dynamic.get("input_tokens"),
        "dynamic_closure_output_tokens": dynamic.get("output_tokens"),
        "dynamic_closure_cache_read_tokens": dynamic.get("cache_read_input_tokens"),
        "dynamic_closure_estimated_cost_usd": dynamic.get("estimated_cost_usd"),
        # Timing + cost. When both stage1/analysis and stage2/generate metrics
        # exist, we sum them so experiment totals reflect the full pipeline.
        "run_metrics_file": metrics_file,
        "wall_clock_seconds": run_metrics.get("wall_clock_seconds"),
        "model": run_metrics.get("model"),
        "total_cost_usd": run_metrics.get("total_cost_usd"),
        "estimated_cost_usd": run_metrics.get("estimated_cost_usd"),
        "end_to_end_estimated_cost_usd": (
            float(run_metrics.get("estimated_cost_usd", 0) or 0)
            + float(dynamic.get("estimated_cost_usd", 0) or 0)
        ),
        "input_tokens": run_metrics.get("input_tokens"),
        "output_tokens": run_metrics.get("output_tokens"),
        "cache_read_tokens": run_metrics.get("cache_read_input_tokens"),
        "cache_create_tokens": run_metrics.get("cache_creation_input_tokens"),
        "analysis_cost_usd": analysis_metrics.get("total_cost_usd"),
        "generate_cost_usd": generate_metrics.get("total_cost_usd"),
        "analysis_estimated_cost_usd": analysis_metrics.get("estimated_cost_usd"),
        "generate_estimated_cost_usd": generate_metrics.get("estimated_cost_usd"),
        "analysis_wall_clock_seconds": analysis_metrics.get("wall_clock_seconds"),
        "generate_wall_clock_seconds": generate_metrics.get("wall_clock_seconds"),
        "analysis_status": analysis_stage.get("status"),
        "analysis_handoff_version": analysis_stage.get("handoff_version"),
        "analysis_handoff_ready": analysis_stage.get("handoff_ready"),
        "model_call_count": len(actual_calls),
        "model_turns": sum(int(row.get("model_turns", 0) or 0) for row in actual_calls),
        "tool_calls": sum(int(row.get("tool_calls", 0) or 0) for row in actual_calls),
        "prompt_chars": sum(int(row.get("prompt_chars", 0) or 0) for row in actual_calls),
        "reflection_calls": sum(row.get("call_reason") == "reflection" for row in actual_calls),
        "structured_retries": sum(int(row.get("retry_count", 0) or 0) for row in actual_calls),
        "timeout_calls": sum("timeout" in str(row.get("exception", "")).lower() for row in actual_calls),
        "closure_conflict_edges": sum(
            len(row.get("conflicts", []) or []) for row in model_calls
            if row.get("component") == "closure-conflicts"
        ),
        # Gold patch complexity
        "gold_files_count": gold_files_count,
        "runner_status_raw": runner_status_raw,
        "runner_status": runner_status_effective,
        "infra_failure": infra_failure,
        "model_infra_failure": model_infra_failure,
        "docker_infra_failure": docker_infra_failure,
        "harness_quality_failure": bool(runner_status_raw == "failed" and not infra_failure),
        "failure_reason": failure_reason_for(
            outputs, resolved, patch_outcome, runner_task, analysis_stage
        ),
        "patch_path": str(outputs / "patch.diff"),
        "eval_summary_path": str(outputs / "eval_result" / "eval_summary.json"),
        "logs_dir": str(outputs / "logs"),
    }
    return row


def normalize_issue_name(value: str) -> str:
    value = str(value).strip()
    if value.startswith("swe_issue_"):
        return value
    if value.isdigit():
        return f"swe_issue_{int(value):03d}"
    return value


def selected_issue_dirs(workdir: Path, issues: list[str] | None, first_n: int | None) -> list[Path]:
    if issues:
        names = [normalize_issue_name(x) for x in issues]
        return [workdir / name for name in names]
    dirs = sorted(p for p in workdir.glob("swe_issue_*") if p.is_dir())
    if first_n is not None:
        dirs = dirs[:first_n]
    return dirs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--eval-results",
        default=None,
        help="Optional path to eval_results.json from swe_bench_pro_eval.py",
    )
    parser.add_argument(
        "--output-dir",
        default=str(EVAL_RESULT_DIR),
        help="Directory to write CSV output",
    )
    parser.add_argument(
        "--output-subdir",
        default=model_output_dir_name(),
        help="Per-issue harness output subdirectory to read",
    )
    parser.add_argument(
        "--first-n",
        type=int,
        default=None,
        help="Only include the first N sorted swe_issue_* directories",
    )
    parser.add_argument(
        "--issues",
        nargs="*",
        default=None,
        help="Specific issue numbers or names to include, e.g. 001 002 swe_issue_003",
    )
    args = parser.parse_args()

    eval_results = load_json(Path(args.eval_results)) if args.eval_results else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for issue_dir in selected_issue_dirs(WORKDIR, args.issues, args.first_n):
        if not issue_dir.is_dir():
            print(f"  {issue_dir.name} | missing issue directory")
            continue
        row = collect_case(issue_dir, eval_results, args.output_subdir)
        if row:
            rows.append(row)
            status = "RESOLVED" if row.get("resolved") else (
                "UNRESOLVED" if row.get("resolved") is False else "NOT_EVALED"
            )
            print(f"  {row['issue_dir']} | {row['instance_id'][:40]} | {row['patch_outcome']} | eval:{status}")

    if not rows:
        print("No cases found.")
        return

    # Write detail CSV
    detail_path = output_dir / "metrics_detail.csv"
    fieldnames = list(rows[0].keys())
    with open(detail_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nDetail CSV -> {detail_path}")

    detail_json_path = output_dir / "metrics_detail.json"
    detail_json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Detail JSON -> {detail_json_path}")

    # Summary stats
    total = len(rows)
    evaled = [r for r in rows if r.get("resolved") is not None]
    resolved = [r for r in evaled if r.get("resolved")]
    infra_failures = [r for r in rows if r.get("infra_failure")]
    patch_outcomes = {}
    for r in rows:
        k = r.get("patch_outcome", "N/A")
        patch_outcomes[k] = patch_outcomes.get(k, 0) + 1
    runner_statuses = {}
    build_gate_outcomes = {}
    for r in rows:
        k = r.get("runner_status") or "missing"
        runner_statuses[k] = runner_statuses.get(k, 0) + 1
        b = r.get("compile_outcome") or "unknown"
        build_gate_outcomes[b] = build_gate_outcomes.get(b, 0) + 1

    by_lang: dict[str, dict] = {}
    for r in rows:
        lang = r.get("language", "unknown")
        if lang not in by_lang:
            by_lang[lang] = {"total": 0, "resolved": 0}
        by_lang[lang]["total"] += 1
        if r.get("resolved"):
            by_lang[lang]["resolved"] += 1

    print(f"\n=== SUMMARY ===")
    print(f"Total cases: {total}")
    print(f"Evaluated:   {len(evaled)}")
    print(f"Resolved:    {len(resolved)} ({100*len(resolved)/len(evaled):.1f}% of evaled)" if evaled else "Resolved: N/A")
    print(f"Model infra failures: {len(infra_failures)}")
    print(f"\nPatch outcome distribution:")
    for k, v in sorted(patch_outcomes.items()):
        print(f"  {k}: {v}")
    print(f"\nRunner status distribution:")
    for k, v in sorted(runner_statuses.items()):
        print(f"  {k}: {v}")
    print(f"\nCompile outcome distribution:")
    for k, v in sorted(build_gate_outcomes.items()):
        print(f"  {k}: {v}")
    print(f"\nResolved rate by language:")
    for lang, stats in sorted(by_lang.items()):
        r = stats["resolved"]
        t = stats["total"]
        print(f"  {lang}: {r}/{t} ({100*r/t:.0f}%)" if t else f"  {lang}: 0/0")

    # Write summary CSV
    summary_path = output_dir / "metrics_summary.csv"
    summary_rows = [
        {"metric": "total_cases", "value": total},
        {"metric": "evaluated_cases", "value": len(evaled)},
        {"metric": "swebench_resolved_cases", "value": len(resolved)},
        {"metric": "swebench_resolved_rate_pct", "value": round(100*len(resolved)/len(evaled), 2) if evaled else 0},
        {"metric": "resolved_cases", "value": len(resolved)},
        {"metric": "resolved_rate_pct", "value": round(100*len(resolved)/len(evaled), 2) if evaled else 0},
        {"metric": "model_infra_failures", "value": len(infra_failures)},
    ]
    for k, v in sorted(patch_outcomes.items()):
        summary_rows.append({"metric": f"harness_patch_outcome_{k}", "value": v})
        summary_rows.append({"metric": f"patch_outcome_{k}", "value": v})
    for k, v in sorted(runner_statuses.items()):
        summary_rows.append({"metric": f"runner_status_{k}", "value": v})
    for k, v in sorted(build_gate_outcomes.items()):
        summary_rows.append({"metric": f"compile_outcome_{k}", "value": v})
    for lang, stats in sorted(by_lang.items()):
        summary_rows.append({"metric": f"resolved_{lang}", "value": f"{stats['resolved']}/{stats['total']}"})

    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "value"])
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"Summary CSV -> {summary_path}")

    summary_json_path = output_dir / "metrics_summary.json"
    summary_json_path.write_text(json.dumps(summary_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Summary JSON -> {summary_json_path}")


if __name__ == "__main__":
    main()
