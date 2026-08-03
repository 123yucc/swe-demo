import json
from pathlib import Path
from src.models.patch import PatchPlan
from src.orchestrator.artifact_verify import verify_patch_artifacts
base=Path('/home/user/demo/workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r17')
repo=Path('/home/user/demo/workdir/swe_issue_024/repo')
plan=PatchPlan.model_validate(json.loads((base/'patch_plan.json').read_text()))
diff=(base/'patch.diff').read_text()
res=verify_patch_artifacts(repo, plan, diff)
print('ok', res.ok)
print('findings', [f.model_dump() for f in res.findings])
