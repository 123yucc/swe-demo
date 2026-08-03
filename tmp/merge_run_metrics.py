import json
import sys
from pathlib import Path

from src.main import merge_retry_run_metrics

prior_path, current_path, output_path = map(Path, sys.argv[1:4])
prior = json.loads(prior_path.read_text(encoding="utf-8"))
current = json.loads(current_path.read_text(encoding="utf-8"))
merged = merge_retry_run_metrics(prior, current)
output_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
