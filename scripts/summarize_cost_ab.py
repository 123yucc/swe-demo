"""Compare paired SWE case outputs by cost and resolved status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _estimated_cost(output: Path) -> float:
    total = 0.0
    for name in ("run_metrics.analysis.json", "run_metrics.json", "dynamic_closure.json"):
        metrics = _load(output / name)
        estimated = metrics.get("estimated_cost_usd")
        if estimated is not None:
            total += float(estimated or 0)
            continue
        input_tokens = int(metrics.get("input_tokens", 0) or 0)
        output_tokens = int(metrics.get("output_tokens", 0) or 0)
        cached = int(metrics.get("cache_read_input_tokens", 0) or 0)
        uncached = max(0, input_tokens - cached)
        total += (uncached * 1.75 + cached * 0.175 + output_tokens * 14) / 1_000_000
    return total


def _result(output: Path) -> tuple[str, bool | None]:
    task = _load(output / "runner_task.json")
    return str(task.get("status", "missing")), task.get("resolved")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, default=Path("workdir"))
    parser.add_argument("--issues", nargs="+", required=True)
    parser.add_argument("--baseline", default="outputs_gpt-5.2")
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()

    rows = []
    for issue in args.issues:
        root = args.workdir / f"swe_issue_{issue}"
        baseline = root / args.baseline
        candidate = root / args.candidate
        a_status, a_resolved = _result(baseline)
        b_status, b_resolved = _result(candidate)
        a_cost = _estimated_cost(baseline)
        b_cost = _estimated_cost(candidate)
        rows.append({
            "issue": issue,
            "baseline_status": a_status,
            "baseline_resolved": a_resolved,
            "baseline_cost_usd": round(a_cost, 6),
            "candidate_status": b_status,
            "candidate_resolved": b_resolved,
            "candidate_cost_usd": round(b_cost, 6),
            "cost_change_pct": (
                round((b_cost / a_cost - 1) * 100, 1) if a_cost else None
            ),
        })
    paired = [row for row in rows if row["candidate_status"] == "success"]
    summary = {
        "rows": rows,
        "paired_success_count": len(paired),
        "baseline_average_cost_usd": round(
            sum(row["baseline_cost_usd"] for row in paired) / len(paired), 6
        ) if paired else None,
        "candidate_average_cost_usd": round(
            sum(row["candidate_cost_usd"] for row in paired) / len(paired), 6
        ) if paired else None,
        "baseline_resolved_rate": round(
            sum(row["baseline_resolved"] is True for row in paired) / len(paired), 4
        ) if paired else None,
        "candidate_resolved_rate": round(
            sum(row["candidate_resolved"] is True for row in paired) / len(paired), 4
        ) if paired else None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
