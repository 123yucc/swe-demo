from __future__ import annotations

import json

from scripts.continue_gpt52_pipeline import aggregate_attempts


def test_aggregate_attempts_counts_each_case_once_per_run(tmp_path):
    first = tmp_path / "run-a"
    second = tmp_path / "run-b"
    first.mkdir()
    second.mkdir()
    (first / "runner.state.json").write_text(
        json.dumps(
            {
                "tasks": {
                    "x:081": {
                        "issue": "swe_issue_081",
                        "status": "infra_failed",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (second / "runner.state.json").write_text(
        json.dumps(
            {
                "tasks": {
                    "x:081": {"issue": "swe_issue_081", "status": "success"},
                    "x:082": {"issue": "swe_issue_082", "status": "running"},
                }
            }
        ),
        encoding="utf-8",
    )

    successful, attempts = aggregate_attempts([first, second], {"081", "082"})

    assert successful == {"081"}
    assert attempts == {"081": 2, "082": 0}
