import json
from pathlib import Path
p = Path('/home/user/demo/workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r17/eval_result/instance_internetarchive__openlibrary-25858f9f0c165df25742acf8309ce909773f0cdd-v13642507b4fc1f8d234172bf8129942da2c2ca26/_output.json')
data = json.loads(p.read_text(encoding='utf-8'))
for t in data['tests']:
    if t.get('status') != 'PASSED':
        print('===', t.get('name') or t.get('nodeid'), t.get('status'), '===')
        for key in ['message','traceback','stderr','stdout','error','raw']:
            val=t.get(key)
            if val:
                text=str(val)
                print(key, text[-1500:])
                break
        else:
            print(t)
