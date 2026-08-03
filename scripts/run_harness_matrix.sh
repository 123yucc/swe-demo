#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <runtime/experiments/<experiment>> [model-name ...]" >&2
  exit 2
fi

experiment_dir=$1
shift
matrix_path="$experiment_dir/matrix.json"
if [ ! -f "$matrix_path" ]; then
  echo "missing frozen matrix: $matrix_path" >&2
  exit 2
fi

experiment=$(python3 - "$matrix_path" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["experiment"])
PY
)

selected=" $* "
for manifest in "$experiment_dir"/manifests/*.json; do
  model=$(basename "$manifest" .json)
  if [ "$#" -gt 0 ] && [[ "$selected" != *" $model "* ]]; then
    continue
  fi
  run_name="$experiment/$model"
  echo "[matrix] model=$model manifest=$manifest run=$run_name"
  bash scripts/remote_run_staged_batch.sh "$manifest" "$run_name"
done
