import json

import pytest

from src.main import merge_retry_run_metrics, validate_analysis_checkpoint


def test_analysis_checkpoint_must_be_closed(tmp_path):
    (tmp_path / "checkpoint.json").write_text(json.dumps({
        "pipeline_state": "Closed",
        "budget_counters": {},
    }), encoding="utf-8")
    assert validate_analysis_checkpoint(tmp_path)["pipeline_state"] == "Closed"


def test_analysis_checkpoint_rejects_forced_failure(tmp_path):
    with pytest.raises(RuntimeError, match="closure-approved"):
        validate_analysis_checkpoint(tmp_path)


def test_closure_retry_metrics_accumulate_prior_time_and_tokens():
    prior = {
        "run_start_utc": "start",
        "run_end_utc": "middle",
        "wall_clock_seconds": 100.0,
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_cost_usd": 1.0,
    }
    current = {
        "run_start_utc": "middle",
        "run_end_utc": "end",
        "wall_clock_seconds": 12.5,
        "input_tokens": 50,
        "output_tokens": 5,
        "total_cost_usd": 0.1,
    }
    merged = merge_retry_run_metrics(prior, current)
    assert merged["run_start_utc"] == "start"
    assert merged["run_end_utc"] == "end"
    assert merged["wall_clock_seconds"] == 112.5
    assert merged["input_tokens"] == 1050
    assert merged["output_tokens"] == 105
    assert merged["total_cost_usd"] == pytest.approx(1.1)
    assert len(merged["segments"]) == 2
