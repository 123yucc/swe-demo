import json
from pathlib import Path
p=Path('workdir/long_term_memory/custom_knowledge.json')
data=json.loads(p.read_text(encoding='utf-8'))
for item in data:
    text=' '.join(str(item.get(k,'')) for k in ['id','title','symptom','guidance'])
    if any(w in text.lower() for w in ['extract', 'config', 'retry', 'shared state', 'canonical']):
        print(item.get('id'), item.get('title'))
        print((item.get('symptom') or '')[:300])
        print((item.get('guidance') or '')[:500])
        print('tags', item.get('tags'))
        print()
