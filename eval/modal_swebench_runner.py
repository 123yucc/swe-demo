#!/usr/bin/env python3
"""Collect generated patches and evaluate them with the official Modal path."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_DIR = REPO_ROOT / "eval" / "SWE-bench_Pro-os"
UPSTREAM_EVAL = UPSTREAM_DIR / "swe_bench_pro_eval.py"
RUN_SCRIPTS = UPSTREAM_DIR / "run_scripts"

sys.path.insert(0, str(REPO_ROOT))

from eval.make_eval_inputs import build_inputs, find_all_issues  # noqa: E402


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"


def _require_modal_credentials() -> None:
    try:
        from modal.config import config
    except ImportError as exc:
        raise RuntimeError(
            "Modal is not installed; run `python3 -m pip install modal` first"
        ) from exc
    if not config.get("token_id") or not config.get("token_secret"):
        raise RuntimeError(
            "Modal credentials are missing; run `modal setup` or export both "
            "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET"
        )


def _expected_instance_ids(patches_path: Path) -> list[str]:
    patches = json.loads(patches_path.read_text(encoding="utf-8"))
    return [str(item["instance_id"]) for item in patches]


def _missing_eval_outputs(result_dir: Path, instance_ids: list[str]) -> list[str]:
    return [
        instance_id
        for instance_id in instance_ids
        if not (result_dir / instance_id / "_output.json").is_file()
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run stage-three SWE-bench Pro evaluation on Modal"
    )
    parser.add_argument("--output-subdir", required=True)
    parser.add_argument("--issues", nargs="+", help="Issue directories, e.g. swe_issue_001")
    parser.add_argument("--all", action="store_true", help="Evaluate every generated patch")
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--num-workers", type=int, default=20)
    parser.add_argument("--redo", action="store_true")
    parser.add_argument("--block-network", action="store_true")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Input and result root; defaults to workdir/modal_eval/<output-subdir>.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Generate Modal inputs and print the command without submitting jobs.",
    )
    args = parser.parse_args()
    if bool(args.issues) == bool(args.all):
        parser.error("choose exactly one of --issues or --all")
    return args


def main() -> int:
    args = parse_args()
    issues = args.issues or find_all_issues(args.output_subdir)
    if not issues:
        raise RuntimeError(f"No patches found under {args.output_subdir}")

    run_dir = (args.run_dir or (
        REPO_ROOT / "workdir" / "modal_eval" / _slug(args.output_subdir)
    )).resolve()
    input_dir = run_dir / "inputs"
    result_dir = run_dir / "results"
    patches_path, samples_path = build_inputs(issues, args.output_subdir, input_dir)

    cmd = [
        sys.executable,
        str(UPSTREAM_EVAL),
        "--raw_sample_path",
        str(samples_path),
        "--patch_path",
        str(patches_path),
        "--output_dir",
        str(result_dir),
        "--dockerhub_username",
        args.dockerhub_username,
        "--scripts_dir",
        str(RUN_SCRIPTS),
        "--num_workers",
        str(args.num_workers),
    ]
    if args.redo:
        cmd.append("--redo")
    if args.block_network:
        cmd.append("--block_network")

    print("Modal evaluation command:")
    print(subprocess.list2cmdline(cmd))
    if args.prepare_only:
        return 0
    _require_modal_credentials()
    completed = subprocess.run(cmd, cwd=UPSTREAM_DIR, check=False)
    if completed.returncode:
        return completed.returncode

    missing = _missing_eval_outputs(
        result_dir, _expected_instance_ids(patches_path)
    )
    if missing:
        print(
            "ERROR: evaluator returned success without real Modal output for: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
