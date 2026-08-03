from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/home/user/demo")
SRC = ROOT / "workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r16"
DST = ROOT / "workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r17"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _deep_search_iterations(log_text: str) -> int:
    seen = [int(m.group(1)) for m in re.finditer(r"deep-search iteration (\d+)/", log_text)]
    return max(seen) if seen else 0


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"source output dir not found: {SRC}")
    DST.mkdir(parents=True, exist_ok=True)

    wm = _read_json(SRC / "working_memory.json")
    actions = list(wm.get("action_history") or [])
    closed_index = None
    for i, action in enumerate(actions):
        if (
            action.get("phase") == "closure-check"
            and action.get("outcome") == "CLOSURE_APPROVED"
        ):
            closed_index = i
            break
    if closed_index is None:
        raise SystemExit("cannot reconstruct stage1 checkpoint: no CLOSURE_APPROVED action")

    # r16's working_memory.json was saved after the failed Stage2 attempt.  For
    # r17 we need the last clean Stage1 Closed memory, not patch-planning
    # feedback or failed patch-generation history.
    wm["action_history"] = actions[: closed_index + 1]
    wm["patch_plan"] = None
    wm["build_error_feedback"] = ""
    wm["evidence_focus_files"] = []

    log_path = SRC / "logs/generate.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    deep_iters = _deep_search_iterations(log_text)

    # Closed-state Stage2 resume does not consume deep-search/rework counters,
    # but preserving the exhausted counts prevents any accidental Stage1 reopen
    # from pretending it has fresh budget.
    counters = {
        "deep_search_iterations_done": deep_iters or 24,
        "rework_rounds_used": 3,
        "rework_rounds_by_req": {},
        "patch_verify_rounds_used": 0,
        "plan_coverage_rounds_used": 0,
        "per_req_unchecked_count": {},
        "closure_failure_streak": 0,
    }

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

    for name in [
        "evidence.json",
        "custom_recommendations.json",
        "ltm_recommendations.json",
        "run_metrics.analysis.json",
        "model_calls.jsonl",
    ]:
        src = SRC / name
        if src.exists():
            shutil.copy2(src, DST / name)

    checkpoint_path = DST / "checkpoint.json"
    _write_json(checkpoint_path, checkpoint)

    analysis_stage = _read_json(SRC / "analysis_stage.json")
    analysis_stage["checkpoint"] = str(checkpoint_path)
    analysis_stage["status"] = "analysis_complete"
    analysis_stage["handoff_version"] = 2
    analysis_stage["handoff_ready"] = True
    _write_json(DST / "analysis_stage.json", analysis_stage)

    _write_json(DST / "working_memory.stage1_closed.json", wm)
    print(f"prepared {DST}")
    print(f"checkpoint {checkpoint_path}")
    print(f"deep_search_iterations_done={counters['deep_search_iterations_done']}")
    print(f"stage1_actions={len(wm['action_history'])}")


if __name__ == "__main__":
    main()
