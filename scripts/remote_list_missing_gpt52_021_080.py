#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

TARGET_ISSUES = [
    21, 25, 26, 31, 32, 38, 39, 42, 47, 49,
    52, 54, 59, 63, 64, 67, 69, 70, 73, 77,
]


def main() -> int:
    rows: list[dict[str, object]] = []
    for n in TARGET_ISSUES:
        issue = f"swe_issue_{n:03d}"
        out = Path("workdir") / issue / "outputs_gpt-5.2"
        patch = (out / "patch.diff").exists()
        eval_ok = (out / "eval_result" / "eval_summary.json").exists()
        if patch and eval_ok:
            continue
        rows.append(
            {
                "issue": issue,
                "patch": patch,
                "eval": eval_ok,
                "output_dir_exists": out.exists(),
            }
        )
    print(json.dumps({"missing_count": len(rows), "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
