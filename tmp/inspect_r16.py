import json, pathlib, sys
base=pathlib.Path('/home/user/demo/workdir/swe_issue_024/outputs_clean-knowledge-gpt5.2-r16')
for name in ['run_metrics.analysis.json','working_memory.json','analysis_stage.json']:
    p=base/name
    print(name, p.exists(), p.stat().st_size if p.exists() else '-')
p=base/'run_metrics.analysis.json'
if p.exists():
    data=json.loads(p.read_text(encoding='utf-8'))
    for k in ['status','pipeline_state','deep_search_iterations_done','rework_rounds_used','total_wall_time_s','wall_time_s','total_input_tokens','input_tokens','total_output_tokens','output_tokens']:
        print(k, data.get(k))
    print('budget_counters', data.get('budget_counters'))
p=base/'analysis_stage.json'
if p.exists():
    data=json.loads(p.read_text(encoding='utf-8'))
    print('analysis_stage', data)
