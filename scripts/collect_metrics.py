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


def get_repatch_rounds(action_history: list[dict]) -> int:
    # repatch outcomes include "BUILD_FAILED_repatch" style entries
    return sum(
        1 for evt in action_history
        if "repatch" in str(evt.get("outcome", "")).lower()
        or (evt.get("phase", "") == "patch-planning" and evt.get("outcome", "") == "REPATCH")
    )


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
            "build_rounds": 0,
            "build_gate_passed": None,
            "build_gate_outcome": "NO_BUILD_LOG",
            "build_system": "unknown",
            "build_error_count": 0,
        }
    # build_verification.json is a list of round dicts
    if isinstance(build_verification, list):
        rounds = build_verification
    else:
        rounds = build_verification.get("rounds", [])
    build_system = rounds[0].get("system", "unknown") if rounds else "unknown"
    # Gate passed = final round has no new build or deterministic errors.
    last = rounds[-1] if rounds else {}
    error_fields = (
        "new_errors",
        "rename_residues",
        "undefined_config_symbols",
        "contract_drift",
        "parallel_impl",
        "removed_symbol_test_refs",
        "go_unexport",
        "config_entry_shape",
        "heuristic_findings",
    )
    error_count = sum(len(last.get(name, []) or []) for name in error_fields)
    passed = (bool(last.get("ok")) and error_count == 0) if rounds else None
    if not rounds:
        outcome = "NO_BUILD_LOG"
    elif last.get("skipped"):
        outcome = "SKIPPED"
    elif last.get("timed_out"):
        outcome = "TIMED_OUT"
    elif last.get("unverifiable") and last.get("toolchain_missing"):
        outcome = "UNVERIFIABLE_TOOLCHAIN_MISSING"
    elif last.get("unverifiable"):
        outcome = "UNVERIFIABLE_FAILURE"
    elif passed:
        outcome = "PASSED"
    else:
        outcome = "FAILED"
    return {
        "build_rounds": len(rounds),
        "build_gate_passed": passed,
        "build_gate_outcome": outcome,
        "build_system": build_system,
        "build_error_count": error_count,
    }


def load_per_case_eval_summary(outputs: Path) -> dict:
    summary = load_json(outputs / "eval_result" / "eval_summary.json")
    return summary if isinstance(summary, dict) else {}


def load_runner_task(outputs: Path) -> dict:
    task = load_json(outputs / "runner_task.json")
    return task if isinstance(task, dict) else {}


def failure_reason_for(outputs: Path, resolved: bool | None, patch_outcome: str, runner_task: dict) -> str:
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
    run_metrics = load_json(outputs / "run_metrics.json") or {}

    # Build verification
    build_info = get_build_gate_info(load_json(outputs / "build_verification.json"))

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
        # Process data
        "requirements_total": len(requirements),
        "verdict_AS_IS_VIOLATED": verdict_dist.get("AS_IS_VIOLATED", 0),
        "verdict_AS_IS_COMPLIANT": verdict_dist.get("AS_IS_COMPLIANT", 0),
        "verdict_TO_BE_MISSING": verdict_dist.get("TO_BE_MISSING", 0),
        "verdict_TO_BE_PARTIAL": verdict_dist.get("TO_BE_PARTIAL", 0),
        "verdict_UNCHECKED": verdict_dist.get("UNCHECKED", 0),
        "deep_search_rounds": get_deep_search_rounds(action_history),
        "rework_rounds": get_rework_rounds(action_history),
        "repatch_rounds": get_repatch_rounds(action_history),
        "llm_invocations": get_llm_invocations(action_history),
        "closure_forced_fail": get_closure_forced_fail(action_history),
        **build_info,
        # Timing + cost (from run_metrics.json, may be empty for older cases)
        "wall_clock_seconds": run_metrics.get("wall_clock_seconds"),
        "model": run_metrics.get("model"),
        "total_cost_usd": run_metrics.get("total_cost_usd"),
        "input_tokens": run_metrics.get("input_tokens"),
        "output_tokens": run_metrics.get("output_tokens"),
        "cache_read_tokens": run_metrics.get("cache_read_input_tokens"),
        "cache_create_tokens": run_metrics.get("cache_creation_input_tokens"),
        # Gold patch complexity
        "gold_files_count": gold_files_count,
        "runner_status": runner_task.get("status"),
        "failure_reason": failure_reason_for(outputs, resolved, patch_outcome, runner_task),
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
    patch_outcomes = {}
    for r in rows:
        k = r.get("patch_outcome", "N/A")
        patch_outcomes[k] = patch_outcomes.get(k, 0) + 1
    runner_statuses = {}
    build_gate_outcomes = {}
    for r in rows:
        k = r.get("runner_status") or "missing"
        runner_statuses[k] = runner_statuses.get(k, 0) + 1
        b = r.get("build_gate_outcome") or "unknown"
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
    print(f"\nPatch outcome distribution:")
    for k, v in sorted(patch_outcomes.items()):
        print(f"  {k}: {v}")
    print(f"\nRunner status distribution:")
    for k, v in sorted(runner_statuses.items()):
        print(f"  {k}: {v}")
    print(f"\nBuild gate outcome distribution:")
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
    ]
    for k, v in sorted(patch_outcomes.items()):
        summary_rows.append({"metric": f"harness_patch_outcome_{k}", "value": v})
        summary_rows.append({"metric": f"patch_outcome_{k}", "value": v})
    for k, v in sorted(runner_statuses.items()):
        summary_rows.append({"metric": f"runner_status_{k}", "value": v})
    for k, v in sorted(build_gate_outcomes.items()):
        summary_rows.append({"metric": f"build_gate_outcome_{k}", "value": v})
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
