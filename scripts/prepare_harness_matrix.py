#!/usr/bin/env python3
"""Freeze a multi-model manifest into auditable single-model manifests."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()


def discover_issues(workdir: Path) -> list[str]:
    return [
        path.name.removeprefix("swe_issue_")
        for path in sorted(workdir.glob("swe_issue_*"))
        if (path / "artifacts" / "instance_metadata.json").is_file()
    ]


def freeze_matrix(manifest_path: Path, workdir: Path, output_root: Path) -> list[Path]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    experiment = slug(str(data.get("experiment") or manifest_path.stem))
    models = data.get("models") or []
    if not models:
        raise ValueError("matrix manifest has no models")

    raw_issues = data.get("issues")
    issues = discover_issues(workdir) if raw_issues == "all" else [str(x) for x in raw_issues or []]
    expected = data.get("expected_issue_count")
    if expected is not None and len(issues) != int(expected):
        raise ValueError(
            f"expected {int(expected)} prepared issues, found {len(issues)} in {workdir}"
        )
    if not issues:
        raise ValueError(f"no prepared issues found in {workdir}")

    target = output_root / experiment / "manifests"
    target.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for model in models:
        name = slug(str(model.get("name") or model.get("model") or "model"))
        frozen = {
            "experiment": experiment,
            "defaults": data.get("defaults") or {},
            "models": [model],
            "issues": issues,
            "expected_issue_count": len(issues),
            "max_workers": data.get("max_workers", 8),
        }
        path = target / f"{name}.json"
        path.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
        outputs.append(path)

    index = {
        "experiment": experiment,
        "source_manifest": str(manifest_path.resolve()),
        "issue_count": len(issues),
        "task_count": len(issues) * len(models),
        "models": [str(path) for path in outputs],
        "log_layout": f"logs/runs/{experiment}/<model>/<phase>/",
    }
    (target.parent / "matrix.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=ROOT / "workdir")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "runtime" / "experiments"
    )
    args = parser.parse_args()
    outputs = freeze_matrix(args.manifest, args.workdir, args.output_root)
    print(f"frozen {len(outputs)} model manifests")
    for path in outputs:
        print(path)


if __name__ == "__main__":
    main()
