from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/home/user/demo")
SRC = ROOT / "workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r17"
DST = ROOT / "workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r23"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    wm_path = SRC / "working_memory.stage1_closed.json"
    if not wm_path.exists():
        raise SystemExit(f"stage1 closed memory not found: {wm_path}")
    DST.mkdir(parents=True, exist_ok=True)
    wm = _read_json(wm_path)
    wm["patch_plan"] = None
    wm["build_error_feedback"] = ""
    wm["evidence_focus_files"] = []

    counters = {
        "deep_search_iterations_done": 24,
        "rework_rounds_used": 3,
        "rework_rounds_by_req": {},
        "patch_verify_rounds_used": 0,
        "plan_coverage_rounds_used": 0,
        "per_req_unchecked_count": {},
        "closure_failure_streak": 0,
    }
    checkpoint_path = DST / "checkpoint.json"
    checkpoint = {
        "version": "1",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "model_backend": "openai",
            "model": "gpt-5.2",
            "api_surface": "responses",
        },
        "pipeline_state": "Closed",
        "ltm_query": (wm.get("issue_context") or "").strip(),
        "custom_route_query": (wm.get("issue_context") or "").strip(),
        "budget_counters": counters,
        "working_memory": wm,
    }
    _write_json(checkpoint_path, checkpoint)
    _write_json(DST / "evidence.json", wm["evidence_cards"])
    _write_json(DST / "working_memory.stage1_closed.json", wm)

    analysis_stage = _read_json(SRC / "analysis_stage.json")
    analysis_stage["checkpoint"] = str(checkpoint_path)
    analysis_stage["status"] = "analysis_complete"
    analysis_stage["handoff_version"] = 2
    analysis_stage["handoff_ready"] = True
    _write_json(DST / "analysis_stage.json", analysis_stage)

    for name in [
        "custom_recommendations.json",
        "ltm_recommendations.json",
        "run_metrics.analysis.json",
    ]:
        src = SRC / name
        if src.exists():
            shutil.copy2(src, DST / name)
    print(f"prepared {DST}")


if __name__ == "__main__":
    main()
