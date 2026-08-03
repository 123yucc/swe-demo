import json
from pathlib import Path
p = Path('/home/user/demo/workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r17/eval_result/instance_internetarchive__openlibrary-25858f9f0c165df25742acf8309ce909773f0cdd-v13642507b4fc1f8d234172bf8129942da2c2ca26/_output.json')
data = json.loads(p.read_text(encoding='utf-8'))
print(data.keys())
for k,v in data.items():
    if isinstance(v, str):
        print(k, len(v), repr(v[:200]), repr(v[-200:]))
    else:
        print(k, type(v), v if k!='tests' else f'tests {len(v)}')
