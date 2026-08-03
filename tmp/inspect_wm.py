import json, pathlib
wm=json.loads(pathlib.Path('/home/user/demo/workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r16/working_memory.json').read_text())
print(wm.keys())
ah=wm.get('action_history') or []
print('actions', len(ah))
for a in ah[-30:]:
    print(a)
reqs=wm.get('evidence_cards',{}).get('requirements',[])
print('reqs', len(reqs))
from collections import Counter
print(Counter(r.get('verdict') for r in reqs))
