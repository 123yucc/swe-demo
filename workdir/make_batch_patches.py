#!/usr/bin/env python3
"""生成批量 patches JSON 和 samples JSONL 的小脚本（更稳、避免 PowerShell 一次性内存问题）。

用法:
  python workdir\make_batch_patches.py

会在 workdir 下写入:
  - batch_001_006_patches.json
  - batch_001_006_samples.jsonl
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ISSUES = [
    'swe_issue_001',
    'swe_issue_002',
    'swe_issue_003',
    'swe_issue_004',
    'swe_issue_005',
    'swe_issue_006',
]

patches_out = ROOT / 'batch_001_006_patches.json'
samples_out = ROOT / 'batch_001_006_samples.jsonl'

patches = []
with samples_out.open('w', encoding='utf-8') as sf:
    for issue in ISSUES:
        inst_path = ROOT / issue / 'artifacts' / 'instance_metadata.json'
        patch_path = ROOT / issue / 'outputs' / 'patch.diff'
        if not inst_path.exists():
            print(f'WARN: missing {inst_path}, skipping')
            continue
        inst = json.loads(inst_path.read_text(encoding='utf-8'))
        if patch_path.exists():
            patch = patch_path.read_text(encoding='utf-8')
        else:
            patch = inst.get('patch', '')

        patches.append({'instance_id': inst.get('instance_id', ''), 'patch': patch})

        sample = {
            'instance_id': inst.get('instance_id', ''),
            'before_repo_set_cmd': inst.get('before_repo_set_cmd', ''),
            'selected_test_files_to_run': inst.get('selected_test_files_to_run', ''),
            'base_commit': inst.get('base_commit', ''),
            'base_dockerfile': inst.get('base_dockerfile', ''),
            'instance_dockerfile': inst.get('instance_dockerfile', ''),
            'repo': inst.get('repo', ''),
            'dockerhub_tag': inst.get('dockerhub_tag', ''),
            'fail_to_pass': inst.get('fail_to_pass', inst.get('FAIL_TO_PASS', '')),
            'pass_to_pass': inst.get('pass_to_pass', inst.get('PASS_TO_PASS', '')),
            'FAIL_TO_PASS': inst.get('FAIL_TO_PASS', inst.get('fail_to_pass', '')),
            'PASS_TO_PASS': inst.get('PASS_TO_PASS', inst.get('pass_to_pass', '')),
            'test_patch': inst.get('test_patch', ''),
            'problem_statement': inst.get('problem_statement', ''),
            'requirements': inst.get('requirements', ''),
            'interface': inst.get('interface', ''),
        }
        sf.write(json.dumps(sample, ensure_ascii=False) + "\n")

with patches_out.open('w', encoding='utf-8') as pf:
    json.dump(patches, pf, ensure_ascii=False)

print('Wrote:', patches_out, samples_out)