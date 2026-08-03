#!/usr/bin/env python3
"""Select only already-evaluated, unresolved GPT-5.2 patches for evaluator reruns."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
SOURCE = ROOT / "eval" / "manifests" / "swebench-pro-081-731.gpt5.2.json"
DEFAULT_ROOT = ROOT / "runtime" / "gpt52-731" / "eval-retry"


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(first: int, last: int, destination: Path, output_subdir: str) -> dict:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    destination.mkdir(parents=True)
    unresolved: list[str] = []
    resolved_baseline: list[dict] = []
    invalid_unresolved: list[str] = []
    missing_eval: list[str] = []

    for label in range(first, last + 1):
        issue = f"{label:03d}"
        output = WORKDIR / f"swe_issue_{issue}" / output_subdir
        summary_path = output / "eval_result" / "eval_summary.json"
        summary = load_json(summary_path)
        if "resolved" not in summary:
            missing_eval.append(issue)
            continue
        if summary.get("resolved") is True:
            resolved_baseline.append(
                {"issue": issue, "path": str(summary_path), "sha256": sha256(summary_path)}
            )
        elif effective_patch(output / "patch.diff"):
            unresolved.append(issue)
        else:
            invalid_unresolved.append(issue)

    manifest = json.loads(SOURCE.read_text(encoding="utf-8"))
    manifest.pop("issue_range", None)
    manifest.pop("batch_size", None)
    manifest["issues"] = unresolved
    manifest["expected_issue_count"] = len(unresolved)
    manifest["max_workers"] = min(2, len(unresolved)) or 1
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    selection = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "range": [first, last],
        "output_subdir": output_subdir,
        "unresolved_with_effective_patch": unresolved,
        "unresolved_without_effective_patch": invalid_unresolved,
        "missing_eval": missing_eval,
        "resolved_eval_baseline": resolved_baseline,
        "manifest": str(manifest_path),
    }
    (destination / "selection.json").write_text(
        json.dumps(selection, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return selection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=731)
    parser.add_argument("--output-subdir", default="outputs_gpt-5.2")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if not 1 <= args.first <= args.last <= 731:
        raise ValueError("expected 1 <= first <= last <= 731")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (args.destination or DEFAULT_ROOT / timestamp).resolve()
    selection = build(args.first, args.last, destination, args.output_subdir)
    print(
        json.dumps(
            {
                "destination": str(destination),
                "retry_count": len(selection["unresolved_with_effective_patch"]),
                "invalid_patch_count": len(selection["unresolved_without_effective_patch"]),
                "resolved_preserved_count": len(selection["resolved_eval_baseline"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
