"""Create reproducible smoke/20-case manifests for deep-search ablations.

This only prepares manifests; it intentionally does not launch expensive model
or SWE-bench evaluation jobs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SMOKE_CASES = ["021", "022", "023", "024", "076"]
FIXED_CASES = [
    "002", "006", "021", "022", "023", "024", "025", "026", "027", "028",
    "030", "031", "034", "038", "043", "048", "057", "058", "067", "076",
]


def prepare(template: dict, cases: list[str], env: dict[str, str]) -> dict:
    available = {str(value).zfill(3) for value in template.get("issues", [])}
    missing = [case for case in cases if case not in available]
    if missing:
        raise ValueError(f"template is missing required cases: {missing}")
    result = json.loads(json.dumps(template))
    result["issues"] = cases
    for model in result.get("models", []):
        model.setdefault("env", {}).update(env)
    result["experiment"] = {
        "case_order": cases,
        "concurrency_policy": "fixed_by_runner_invocation",
        "quality_gates_unchanged": True,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--subset", choices=["smoke", "fixed20"], default="smoke")
    parser.add_argument("--reflection", choices=["base", "none", "rule"], default="rule")
    parser.add_argument("--responses-state", choices=["stateless", "stateful"], default="stateless")
    parser.add_argument("--batch-mode", choices=["single", "adaptive"], default="single")
    args = parser.parse_args()
    template = json.loads(args.template.read_text(encoding="utf-8"))
    cases = SMOKE_CASES if args.subset == "smoke" else FIXED_CASES
    result = prepare(template, cases, {
        "DEEP_SEARCH_REFLECTION_MODE": args.reflection,
        "OPENAI_RESPONSES_STATE_MODE": args.responses_state,
        "DEEP_SEARCH_BATCH_MODE": args.batch_mode,
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
