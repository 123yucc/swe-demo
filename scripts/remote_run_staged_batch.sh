#!/usr/bin/env bash
set -u

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <manifest-path> <batch-name>" >&2
  echo "env overrides: ANALYSIS_WORKERS STAGE2_WORKERS PHASE3_WORKERS HEAVY_WORKERS NORMAL_PER_TASK_GB HEAVY_PER_TASK_GB HEAVY_ISSUES STAGED_PLAN_ONLY" >&2
  exit 2
fi

manifest_path=$1
batch_name=$2

script_dir=$(cd "$(dirname "$0")" && pwd)
if [ -f "$script_dir/.env" ] && [ -d "$script_dir/eval" ]; then
  repo_root=$script_dir
else
  repo_root=$(cd "$script_dir/.." && pwd)
fi
cd "$repo_root"

readarray -t capacity_defaults < <(python3 - "$manifest_path" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
models = data.get("models") or []
model = models[0] if models else {}
backend = str(model.get("backend") or "").lower()
analysis_fallback = 16 if backend in {"anthropic", "claude"} else 8
capacity = dict((data.get("defaults") or {}).get("capacity") or {})
capacity.update(model.get("capacity") or {})
for key, fallback in (
    ("analysis_workers", analysis_fallback),
    ("stage2_workers", 3),
    ("phase3_workers", 3),
    ("heavy_workers", 2),
    ("normal_per_task_gb", 6),
    ("heavy_per_task_gb", 8),
):
    print(capacity.get(key, fallback))
PY
)

analysis_workers=${ANALYSIS_WORKERS:-${capacity_defaults[0]}}
stage2_workers=${STAGE2_WORKERS:-${capacity_defaults[1]}}
phase3_workers=${PHASE3_WORKERS:-${capacity_defaults[2]}}
heavy_workers=${HEAVY_WORKERS:-${capacity_defaults[3]}}
normal_per_task_gb=${NORMAL_PER_TASK_GB:-${PER_TASK_GB:-${capacity_defaults[4]}}}
heavy_per_task_gb=${HEAVY_PER_TASK_GB:-${capacity_defaults[5]}}
heavy_issues=${HEAVY_ISSUES:-023,024,076,081,082,083,084,085,086,087,090,092,094,095,096,098,099,100}
allow_failed_patch_eval=${ALLOW_FAILED_PATCH_EVAL:-0}

runtime_dir="$repo_root/runtime/staged/$batch_name"
mkdir -p "$runtime_dir"

normal_manifest="$runtime_dir/normal.json"
heavy_manifest="$runtime_dir/heavy.json"
meta_json="$runtime_dir/manifest_meta.json"

python3 - "$manifest_path" "$normal_manifest" "$heavy_manifest" "$meta_json" "$heavy_issues" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
normal_path = Path(sys.argv[2])
heavy_path = Path(sys.argv[3])
meta_path = Path(sys.argv[4])
heavy_csv = sys.argv[5]

data = json.loads(manifest_path.read_text(encoding="utf-8"))
issues = [str(x).zfill(3) for x in data.get("issues", [])]
heavy_set = {item.strip().zfill(3) for item in heavy_csv.split(",") if item.strip()}
heavy = [issue for issue in issues if issue in heavy_set]
normal = [issue for issue in issues if issue not in heavy_set]

base = dict(data)
base.pop("tasks", None)

normal_doc = dict(base)
normal_doc["issues"] = normal
normal_doc["max_workers"] = len(normal) or 1

heavy_doc = dict(base)
heavy_doc["issues"] = heavy
heavy_doc["max_workers"] = len(heavy) or 1

normal_path.write_text(json.dumps(normal_doc, indent=2), encoding="utf-8")
heavy_path.write_text(json.dumps(heavy_doc, indent=2), encoding="utf-8")
meta_path.write_text(
    json.dumps(
        {
            "all_issues": issues,
            "normal_issues": normal,
            "heavy_issues": heavy,
            "output_subdir": data["models"][0]["output_subdir"],
        },
        indent=2,
    ),
    encoding="utf-8",
)
print(json.dumps({"normal": len(normal), "heavy": len(heavy)}))
PY

readarray -t manifest_meta < <(python3 - "$meta_json" <<'PY'
import json
import sys
from pathlib import Path
meta = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(meta["output_subdir"])
print(len(meta["all_issues"]))
print(",".join(meta["normal_issues"]))
print(",".join(meta["heavy_issues"]))
PY
)

output_subdir=${manifest_meta[0]}
issue_count=${manifest_meta[1]}
normal_issues_csv=${manifest_meta[2]}
heavy_issues_csv=${manifest_meta[3]}

echo "[batch] name=$batch_name manifest=$manifest_path"
echo "[batch] analysis_workers=$analysis_workers stage2_workers=$stage2_workers phase3_workers=$phase3_workers heavy_workers=$heavy_workers normal_per_task_gb=$normal_per_task_gb heavy_per_task_gb=$heavy_per_task_gb"
echo "[batch] normal_issues=${normal_issues_csv:-<none>}"
echo "[batch] heavy_issues=${heavy_issues_csv:-<none>}"

if [ "${STAGED_PLAN_ONLY:-0}" = "1" ]; then
  echo "[batch-plan-only] no runners started"
  exit 0
fi

phase_rcs=()

registry_ready() {
  local max_attempts=${STAGED_REGISTRY_PROBE_MAX_ATTEMPTS:-4}
  local retry_delay=${STAGED_REGISTRY_PROBE_RETRY_DELAY_SECONDS:-10}
  local attempt=1
  local http_code
  while [ "$attempt" -le "$max_attempts" ]; do
    http_code=$(
      timeout 20 curl \
        --proxy http://127.0.0.1:7897 \
        --silent --show-error \
        --output /dev/null \
        --write-out '%{http_code}' \
        https://registry-1.docker.io/v2/ || true
    )
    if [ "$http_code" = "401" ]; then
      return 0
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      echo "[infra-probe-retry] docker registry returned ${http_code:-no-response} attempt=$attempt next_attempt=$((attempt + 1)) delay_seconds=$retry_delay"
      sleep "$retry_delay"
    fi
    attempt=$((attempt + 1))
  done
  echo "[infra-circuit-open] docker registry returned ${http_code:-no-response} attempts=$max_attempts"
  return 1
}

wait_for_runner() {
  local run_name=$1
  local run_dir="$repo_root/logs/runs/$run_name"
  local pid_path="$run_dir/runner.pid"
  local status_path="$run_dir/runner.status"
  local state_path="$run_dir/runner.state.json"

  while true; do
    if [ -f "$status_path" ]; then
      local rc
      rc=$(cat "$status_path" 2>/dev/null || echo 1)
      echo "[phase-done] run=$run_name rc=$rc"
      if [ -f "$state_path" ]; then
        python3 - "$state_path" <<'PY' || true
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    tasks = json.loads(path.read_text(encoding="utf-8")).get("tasks", {})
except Exception:
    print("[state] unavailable")
    raise SystemExit(0)
counts = {}
for task in tasks.values():
    status = str(task.get("status") or "unknown")
    counts[status] = counts.get(status, 0) + 1
print("[state]", json.dumps(counts, ensure_ascii=False, sort_keys=True))
PY
      fi
      return "$rc"
    fi

    if [ -f "$pid_path" ]; then
      local pid
      pid=$(cat "$pid_path" 2>/dev/null || true)
      if [ -n "${pid:-}" ] && kill -0 "$pid" >/dev/null 2>&1; then
        if [ -f "$state_path" ]; then
          python3 - "$state_path" "$run_name" <<'PY' || true
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
run_name = sys.argv[2]
try:
    tasks = json.loads(path.read_text(encoding="utf-8")).get("tasks", {})
except Exception:
    print(f"[phase-wait] run={run_name} state=unreadable")
    raise SystemExit(0)
counts = {}
for task in tasks.values():
    status = str(task.get("status") or "unknown")
    counts[status] = counts.get(status, 0) + 1
print(f"[phase-wait] run={run_name} counts=" + json.dumps(counts, ensure_ascii=False, sort_keys=True))
PY
        else
          echo "[phase-wait] run=$run_name state=pending"
        fi
        sleep 60
        continue
      fi
    fi

    echo "[phase-error] run=$run_name exited without status file"
    return 1
  done
}

start_and_wait() {
  local manifest=$1
  local base_run_name=$2
  shift 2

  if [ ! -s "$manifest" ]; then
    echo "[phase-error] run=$base_run_name manifest missing or empty: $manifest"
    phase_rcs+=("1")
    return 0
  fi

  local task_count
  if ! task_count=$(python3 - "$manifest" <<'PY'
import json
import sys
from pathlib import Path
print(len(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("issues", [])))
PY
  ); then
    echo "[phase-error] run=$base_run_name manifest unreadable: $manifest"
    phase_rcs+=("1")
    return 0
  fi
  if ! [[ "$task_count" =~ ^[0-9]+$ ]]; then
    echo "[phase-error] run=$base_run_name invalid task count: $task_count"
    phase_rcs+=("1")
    return 0
  fi
  if [ "$task_count" -eq 0 ]; then
    echo "[phase-skip] run=$base_run_name reason=empty-manifest"
    phase_rcs+=("0")
    return 0
  fi

  if [[ " $* " != *" --phase analysis "* ]] && ! registry_ready; then
    phase_rcs+=("75")
    return 0
  fi

  local max_attempts=${STAGED_INFRA_MAX_ATTEMPTS:-3}
  local retry_delay=${STAGED_INFRA_RETRY_DELAY_SECONDS:-60}
  local attempt=1
  local rc=1
  while [ "$attempt" -le "$max_attempts" ]; do
    local run_name
    run_name="${base_run_name}-a$(printf '%02d' "$attempt")"
    echo "[phase-start] run=$run_name manifest=$manifest args=$*"
    if ! bash scripts/remote_start_runner.sh "$manifest" "$run_name" "$@"; then
      echo "[phase-error] run=$run_name launcher failed"
      rc=1
      break
    fi
    # wait_for_runner legitimately returns 1 for patch/eval failures and 75 for
    # retryable infrastructure failures.  Keep those return codes as data:
    # under `set -e`, a bare call here would abort the whole staged batch before
    # phase3 and the remaining classifications/metrics can run.
    if wait_for_runner "$run_name"; then
      rc=0
    else
      rc=$?
    fi
    if [ "$rc" -ne 75 ] || [ "$attempt" -ge "$max_attempts" ]; then
      break
    fi
    echo "[phase-retry] run=$run_name reason=retryable-infra next_attempt=$((attempt + 1)) delay_seconds=$retry_delay"
    sleep "$retry_delay"
    attempt=$((attempt + 1))
  done
  phase_rcs+=("$rc")
  return 0
}

filter_ready_manifest() {
  local source_manifest=$1
  local target_manifest=$2
  local readiness=$3
  python3 - "$source_manifest" "$target_manifest" "$readiness" <<'PY'
import json
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
readiness = sys.argv[3]
allow_failed_patch_eval = os.environ.get("ALLOW_FAILED_PATCH_EVAL") == "1"
repo_root = Path.cwd()
data = json.loads(source.read_text(encoding="utf-8"))
output_subdir = data["models"][0]["output_subdir"]

def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def effective_patch(path):
    try:
        return any(line.startswith("diff --git ") for line in path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines())
    except OSError:
        return False

def stage2_ready(out):
    outcome = load(out / "patch_outcome.json") or {}
    return (
        effective_patch(out / "patch.diff")
        and (out / "compile_check.json").is_file()
        and outcome.get("patch_outcome") in {"PATCH_SUCCESS", "BUILD_UNVERIFIABLE"}
    )

def final_failed_patch_ready(out):
    outcome = load(out / "patch_outcome.json") or {}
    return (
        allow_failed_patch_eval
        and effective_patch(out / "patch.diff")
        and (out / "compile_check.json").is_file()
        and outcome.get("patch_outcome") in {
            "BUILD_FAILED",
            "BUILD_FAILED_NO_REPAIR",
            "BUILD_FAILED_AFTER_REPAIR",
            "PATCH_FAILED",
            "PATCH_INCOMPLETE",
            "PARTIAL_PATCH",
        }
    )

def analysis_ready(out):
    stage = load(out / "analysis_stage.json") or {}
    checkpoint = load(out / "checkpoint.json") or {}
    saved_checkpoint = load(out / "checkpoint.analysis_handoff.json") or {}
    return (
        stage2_ready(out)
        or (
            stage.get("status") == "analysis_complete"
            and checkpoint.get("pipeline_state") == "Closed"
        )
        or (
            saved_checkpoint.get("pipeline_state") == "Closed"
            and (out / "evidence.analysis_handoff.json").is_file()
        )
    )

ready = []
for raw_issue in data.get("issues", []):
    issue = str(raw_issue).zfill(3)
    out = repo_root / "workdir" / f"swe_issue_{issue}" / output_subdir
    if (
        analysis_ready(out)
        if readiness == "analysis"
        else stage2_ready(out) or final_failed_patch_ready(out)
    ):
        ready.append(issue)

data["issues"] = ready
data["expected_issue_count"] = len(ready)
data["max_workers"] = len(ready) or 1
temporary = target.with_suffix(target.suffix + ".tmp")
temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
os.replace(temporary, target)
print(f"[filter] readiness={readiness} source={source.name} ready={len(ready)}")
PY
}

stage2_normal_manifest="$runtime_dir/stage2-normal-ready.json"
stage2_heavy_manifest="$runtime_dir/stage2-heavy-ready.json"
phase3_normal_manifest="$runtime_dir/phase3-normal-ready.json"
phase3_heavy_manifest="$runtime_dir/phase3-heavy-ready.json"
normal_owned_images="$runtime_dir/owned-images-normal.json"
heavy_owned_images="$runtime_dir/owned-images-heavy.json"
phase3_final_args=()
if [ "${allow_failed_patch_eval:-0}" = "1" ]; then
  phase3_final_args+=(--allow-failed-patch-eval)
  echo "[final-pass] phase3 will evaluate effective failed-build patches after recovery exhaustion"
fi

cleanup_owned_images() {
  local ledger=$1
  if [ ! -f "$ledger" ]; then
    return 0
  fi
  echo "[owned-image-cleanup] ledger=$ledger"
  if ! python3 scripts/cleanup_owned_docker_images.py "$ledger"; then
    echo "[owned-image-cleanup] one or more batch-owned images could not be removed"
    phase_rcs+=("1")
  fi
}

start_and_wait "$manifest_path" "${batch_name}-analysis" \
  --phase analysis \
  --max-workers "$analysis_workers" \
  --retry-failed-closure

filter_ready_manifest "$normal_manifest" "$stage2_normal_manifest" analysis
start_and_wait "$stage2_normal_manifest" "${batch_name}-stage2-normal" \
  --phase stage2 \
  --max-workers "$stage2_workers" \
  --per-task-gb "$normal_per_task_gb" \
  --owned-images-file "$normal_owned_images"

filter_ready_manifest "$stage2_normal_manifest" "$phase3_normal_manifest" stage2
start_and_wait "$phase3_normal_manifest" "${batch_name}-phase3-normal" \
  --phase phase3 \
  --max-workers "$phase3_workers" \
  --per-task-gb "$normal_per_task_gb" \
  "${phase3_final_args[@]}" \
  --owned-images-file "$normal_owned_images"

cleanup_owned_images "$normal_owned_images"

filter_ready_manifest "$heavy_manifest" "$stage2_heavy_manifest" analysis
start_and_wait "$stage2_heavy_manifest" "${batch_name}-stage2-heavy" \
  --phase stage2 \
  --max-workers "$heavy_workers" \
  --per-task-gb "$heavy_per_task_gb" \
  --owned-images-file "$heavy_owned_images"

filter_ready_manifest "$stage2_heavy_manifest" "$phase3_heavy_manifest" stage2
start_and_wait "$phase3_heavy_manifest" "${batch_name}-phase3-heavy" \
  --phase phase3 \
  --max-workers "$heavy_workers" \
  --per-task-gb "$heavy_per_task_gb" \
  "${phase3_final_args[@]}" \
  --owned-images-file "$heavy_owned_images"

cleanup_owned_images "$heavy_owned_images"

metrics_dir="workdir/eval_result_${batch_name}"
python3 scripts/collect_metrics.py \
  --output-subdir "$output_subdir" \
  --issues ${normal_issues_csv//,/ } ${heavy_issues_csv//,/ } \
  --output-dir "$metrics_dir" || phase_rcs+=("1")

overall_rc=0
for rc in "${phase_rcs[@]}"; do
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 75 ]; then
    overall_rc=1
    break
  fi
  if [ "$rc" -eq 75 ]; then
    overall_rc=75
  fi
done

echo "[batch-done] name=$batch_name rc=$overall_rc metrics_dir=$metrics_dir"
exit "$overall_rc"
