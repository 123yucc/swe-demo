#!/usr/bin/env python3
"""Classify SWE-bench harness exits from runner artifacts.

This script scans per-issue runner outputs and summarizes whether a case:
- never reached harness preflight,
- passed Python/bootstrap preflight,
- reached src.main,
- or failed for a likely non-bootstrap reason such as OOM or proxy issues.

It is intended for server-side triage after a concurrent batch run.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SIGNATURE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("no_python311", re.compile(r"No usable Python 3\.11 runtime found")),
    ("pip_bootstrap_failed", re.compile(r"pip bootstrap failed", re.IGNORECASE)),
    ("wheelhouse_missing", re.compile(r"wheelhouse is missing", re.IGNORECASE)),
    (
        "incompatible_wheel",
        re.compile(r"(not a supported wheel on this platform|No matching distribution found)", re.IGNORECASE),
    ),
    ("missing_go_toolchain", re.compile(r"Go repository but go toolchain is not visible", re.IGNORECASE)),
    ("entrypoint_dash_c", re.compile(r'exec: "-c": executable file not found')),
    ("proxyconnect_refused", re.compile(r"proxyconnect tcp:.*connect: connection refused", re.IGNORECASE)),
    ("permission_error", re.compile(r"PermissionError:?\s*\(?13\)?", re.IGNORECASE)),
    ("oom_or_137", re.compile(r"(patch generation container exited 137|\bexit 137\b|\bOOM\b|Killed)", re.IGNORECASE)),
]


@dataclass
class CaseSummary:
    issue: str
    status: str
    phase: str
    signature: str
    same_as_021: str
    recommendation: str
    bootstrap_ok: bool
    requirements_ok: bool
    compat_wheels_logged: bool
    entered_main: bool
    runner_error: str
    generate_log: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize early harness exits from local runner artifacts")
    parser.add_argument("--workdir", type=Path, default=Path("workdir"))
    parser.add_argument("--output-subdir", required=True, help="Per-model output dir name, e.g. outputs_gpt-5.2")
    parser.add_argument("--issues", nargs="*", default=None, help="Optional issue labels, e.g. 021 022 080")
    parser.add_argument("--start", type=int, default=None, help="Optional inclusive start label")
    parser.add_argument("--end", type=int, default=None, help="Optional inclusive end label")
    parser.add_argument("--json", type=Path, default=None, help="Optional path to write JSON summary")
    return parser.parse_args()


def normalize_issue_label(raw: str) -> str:
    value = str(raw).strip()
    if value.startswith("swe_issue_"):
        return value[-3:]
    return f"{int(value):03d}"


def iter_issue_labels(args: argparse.Namespace) -> list[str]:
    if args.issues:
        return [normalize_issue_label(item) for item in args.issues]
    if args.start is not None or args.end is not None:
        if args.start is None or args.end is None:
            raise SystemExit("--start and --end must be provided together")
        return [f"{n:03d}" for n in range(args.start, args.end + 1)]
    labels: list[str] = []
    for path in sorted(args.workdir.glob("swe_issue_*")):
        suffix = path.name.rsplit("_", 1)[-1]
        if suffix.isdigit():
            labels.append(f"{int(suffix):03d}")
    return labels


def safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def safe_read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def first_signature(*texts: str) -> str:
    merged = "\n".join(text for text in texts if text)
    for name, pattern in SIGNATURE_PATTERNS:
        if pattern.search(merged):
            return name
    return "none"


def classify_case(issue_dir: Path, output_subdir: str) -> CaseSummary:
    output_dir = issue_dir / output_subdir
    task_json = safe_read_json(output_dir / "runner_task.json")
    status = str(task_json.get("status") or ("missing_output" if not output_dir.exists() else "unknown"))
    runner_error = str(task_json.get("error") or "")
    generate_log_path = output_dir / "logs" / "generate.log"
    generate_text = safe_read_text(generate_log_path)

    bootstrap_ok = "[harness-preflight] python=" in generate_text
    requirements_ok = "[harness-preflight] requirements=" in generate_text
    compat_wheels_logged = "[harness-preflight] compat_wheels=" in generate_text
    entered_main = any(
        marker in generate_text
        for marker in (
            "[repo-init] Repo prepared",
            "[orchestrator] Running parser agent",
            "[ltm] experience_server already running on port 9030",
        )
    )

    signature = first_signature(generate_text, runner_error)
    phase = "missing_output"
    if output_dir.exists():
        if entered_main:
            phase = "entered_main"
        elif requirements_ok:
            phase = "post_requirements_pre_main"
        elif bootstrap_ok:
            phase = "post_python_pre_requirements"
        elif generate_text:
            phase = "pre_python_preflight"
        else:
            phase = "no_generate_log"

    same_as_021 = "unknown"
    recommendation = "inspect_generate_log"
    if entered_main:
        same_as_021 = "already_resolved_like_021"
        recommendation = "not a pre-main bootstrap failure"
    elif signature in {"no_python311", "pip_bootstrap_failed", "incompatible_wheel"}:
        same_as_021 = "likely"
        recommendation = "retest with current local_swebench_runner.py"
    elif signature in {"oom_or_137", "proxyconnect_refused", "permission_error"}:
        same_as_021 = "unlikely"
        if signature == "oom_or_137":
            recommendation = "reduce concurrency or inspect host memory pressure"
        elif signature == "proxyconnect_refused":
            recommendation = "check Docker proxy tunnel and docker pull reachability"
        else:
            recommendation = "normalize ownership or inspect workspace permissions"
    elif signature in {"wheelhouse_missing", "missing_go_toolchain", "entrypoint_dash_c"}:
        same_as_021 = "unlikely"
        recommendation = "fix runner/container environment before rerun"
    elif bootstrap_ok and not entered_main:
        same_as_021 = "maybe"
        recommendation = "inspect dependency install section around requirements"

    return CaseSummary(
        issue=issue_dir.name.rsplit("_", 1)[-1],
        status=status,
        phase=phase,
        signature=signature,
        same_as_021=same_as_021,
        recommendation=recommendation,
        bootstrap_ok=bootstrap_ok,
        requirements_ok=requirements_ok,
        compat_wheels_logged=compat_wheels_logged,
        entered_main=entered_main,
        runner_error=runner_error,
        generate_log=str(generate_log_path) if generate_log_path.exists() else "",
    )


def render_table(rows: Iterable[CaseSummary]) -> str:
    headers = [
        "issue",
        "status",
        "phase",
        "signature",
        "same_as_021",
        "recommendation",
    ]
    items = [headers]
    for row in rows:
        items.append([
            row.issue,
            row.status,
            row.phase,
            row.signature,
            row.same_as_021,
            row.recommendation,
        ])
    widths = [max(len(str(row[i])) for row in items) for i in range(len(headers))]
    lines = []
    for idx, row in enumerate(items):
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    labels = iter_issue_labels(args)
    summaries: list[CaseSummary] = []
    for label in labels:
        issue_dir = args.workdir / f"swe_issue_{label}"
        summaries.append(classify_case(issue_dir, args.output_subdir))

    print(render_table(summaries))
    print()
    print("counts_by_signature", dict(Counter(item.signature for item in summaries)))
    print("counts_by_phase", dict(Counter(item.phase for item in summaries)))
    print("counts_by_same_as_021", dict(Counter(item.same_as_021 for item in summaries)))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(item) for item in summaries]
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print("json_written", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
