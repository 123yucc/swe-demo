from pathlib import Path
from src.memory.custom_route import load_custom_rules
text = Path('/home/user/demo/workdir/long_term_memory/custom_knowledge.json').read_text(encoding='utf-8')
patterns = ['openlibrary','solr','update_work','SolrUpdateState','024','internetarchive','plugin_worksearch']
hits = [p for p in patterns if p in text]
print('hits', hits)
print('rules', len(load_custom_rules()))
