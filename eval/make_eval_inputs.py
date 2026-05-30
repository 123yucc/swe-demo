#!/usr/bin/env python3
"""生成评测所需的 patches.json 和 samples.jsonl。

用法:
  # 指定 issue 列表
  python eval/make_eval_inputs.py --issues swe_issue_001 swe_issue_002

  # 自动扫描所有有 patch 的 issue
  python eval/make_eval_inputs.py --all

输出到 workdir/eval_result/ 下:
  - patches.json
  - samples.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = REPO_ROOT / "workdir"
OUTPUT_DIR = WORKDIR / "eval_result"


def find_all_issues() -> list[str]:
    """扫描 workdir 下所有含 patch 的 swe_issue_* 目录。"""
    issues = []
    for d in sorted(WORKDIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("swe_issue_"):
            continue
        inst_path = d / "artifacts" / "instance_metadata.json"
        patch_path = d / "outputs" / "patch.diff"
        if inst_path.exists() and (patch_path.exists() or inst_path.exists()):
            issues.append(d.name)
    return issues


def build_inputs(issues: list[str]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    patches_out = OUTPUT_DIR / "patches.json"
    samples_out = OUTPUT_DIR / "samples.jsonl"

    patches = []
    samples_lines = []

    for issue in issues:
        inst_path = WORKDIR / issue / "artifacts" / "instance_metadata.json"
        patch_path = WORKDIR / issue / "outputs" / "patch.diff"

        if not inst_path.exists():
            print(f"WARN: missing {inst_path}, skipping")
            continue

        inst = json.loads(inst_path.read_text(encoding="utf-8"))

        if patch_path.exists():
            patch = patch_path.read_text(encoding="utf-8")
        else:
            patch = inst.get("patch", "")

        if not patch.strip():
            print(f"WARN: empty patch for {issue}, skipping")
            continue

        instance_id = inst.get("instance_id", "")
        patches.append({"instance_id": instance_id, "patch": patch})

        sample = {
            "instance_id": instance_id,
            "before_repo_set_cmd": inst.get("before_repo_set_cmd", ""),
            "selected_test_files_to_run": inst.get("selected_test_files_to_run", ""),
            "base_commit": inst.get("base_commit", ""),
            "base_dockerfile": inst.get("base_dockerfile", ""),
            "instance_dockerfile": inst.get("instance_dockerfile", ""),
            "repo": inst.get("repo", ""),
            "dockerhub_tag": inst.get("dockerhub_tag", ""),
            "fail_to_pass": inst.get("fail_to_pass", inst.get("FAIL_TO_PASS", "")),
            "pass_to_pass": inst.get("pass_to_pass", inst.get("PASS_TO_PASS", "")),
            "FAIL_TO_PASS": inst.get("FAIL_TO_PASS", inst.get("fail_to_pass", "")),
            "PASS_TO_PASS": inst.get("PASS_TO_PASS", inst.get("pass_to_pass", "")),
            "test_patch": inst.get("test_patch", ""),
            "problem_statement": inst.get("problem_statement", ""),
            "requirements": inst.get("requirements", ""),
            "interface": inst.get("interface", ""),
        }
        samples_lines.append(json.dumps(sample, ensure_ascii=False))

    with patches_out.open("w", encoding="utf-8") as f:
        json.dump(patches, f, ensure_ascii=False)

    with samples_out.open("w", encoding="utf-8") as f:
        f.write("\n".join(samples_lines) + "\n")

    print(f"Generated {len(patches)} entries:")
    print(f"  patches: {patches_out}")
    print(f"  samples: {samples_out}")


def main():
    parser = argparse.ArgumentParser(description="生成评测输入文件")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issues", nargs="+", help="指定 issue 目录名列表")
    group.add_argument("--all", action="store_true", help="自动扫描所有有 patch 的 issue")
    args = parser.parse_args()

    if args.all:
        issues = find_all_issues()
        if not issues:
            print("ERROR: no issues with patches found in workdir/")
            sys.exit(1)
        print(f"Found {len(issues)} issues: {issues}")
    else:
        issues = args.issues

    build_inputs(issues)


if __name__ == "__main__":
    main()
