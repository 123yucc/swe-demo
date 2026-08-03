"""Prepare disjoint productive shards for the GPT stage-1 capacity ramp."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SHARDS = {
    "base6": ["021", "022", "023", "024", "034", "076"],
    "add2_to8": ["002", "006"],
    "add2_to10": ["025", "026"],
    "add2_to12": ["027", "028"],
    "remainder8": ["030", "031", "038", "043", "048", "057", "058", "067"],
}


def prepare(template: dict, shard: str) -> dict:
    result = json.loads(json.dumps(template))
    result["issues"] = SHARDS[shard]
    result["max_workers"] = len(SHARDS[shard])
    result["capacity_experiment"] = {
        "shard": shard,
        "disjoint": True,
        "phase": "analysis",
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    template = json.loads(args.template.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for shard in SHARDS:
        path = args.output_dir / f"capacity.{shard}.json"
        path.write_text(json.dumps(prepare(template, shard), indent=2), encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
