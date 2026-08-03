#!/usr/bin/env python3
"""Generate SWE-bench Pro eval input files from per-issue harness outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.output_paths import model_output_dir_name  # noqa: E402

WORKDIR = REPO_ROOT / "workdir"
OUTPUT_DIR = WORKDIR / "eval_result"


def resolve_outputs_dir(issue_dir: Path, output_subdir: str | None) -> Path:
    return issue_dir / (output_subdir or model_output_dir_name())


def find_all_issues(output_subdir: str | None) -> list[str]:
    """Scan workdir for issue directories with a generated patch."""
    issues: list[str] = []
    for d in sorted(WORKDIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("swe_issue_"):
            continue
        inst_path = d / "artifacts" / "instance_metadata.json"
        patch_path = resolve_outputs_dir(d, output_subdir) / "patch.diff"
        if inst_path.exists() and patch_path.exists():
            issues.append(d.name)
    return issues


def build_inputs(
    issues: list[str],
    output_subdir: str | None,
    output_dir: Path = OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    patches_out = output_dir / "patches.json"
    samples_out = output_dir / "samples.jsonl"

    patches: list[dict[str, str]] = []
    samples_lines: list[str] = []

    for issue in issues:
        issue_dir = WORKDIR / issue
        inst_path = issue_dir / "artifacts" / "instance_metadata.json"
        patch_path = resolve_outputs_dir(issue_dir, output_subdir) / "patch.diff"

        if not inst_path.exists():
            print(f"WARN: missing {inst_path}, skipping")
            continue

        inst = json.loads(inst_path.read_text(encoding="utf-8"))

        if not patch_path.exists():
            print(f"WARN: missing generated patch {patch_path}, skipping")
            continue
        patch = patch_path.read_text(encoding="utf-8")

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
    print(f"  output subdir: {output_subdir or model_output_dir_name()}")
    print(f"  patches: {patches_out}")
    print(f"  samples: {samples_out}")
    return patches_out, samples_out


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SWE-bench Pro eval inputs")
    parser.add_argument(
        "--output-subdir",
        default=None,
        help=(
            "Per-issue harness output subdirectory to read. "
            "Defaults to outputs_<current-model>."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Directory for patches.json and samples.jsonl.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issues", nargs="+", help="Issue directory names")
    group.add_argument("--all", action="store_true", help="Scan all issues")
    args = parser.parse_args()

    if args.all:
        issues = find_all_issues(args.output_subdir)
        if not issues:
            print("ERROR: no issues with patches found in workdir/")
            sys.exit(1)
        print(f"Found {len(issues)} issues: {issues}")
    else:
        issues = args.issues

    build_inputs(issues, args.output_subdir, args.output_dir)


if __name__ == "__main__":
    main()
