#!/usr/bin/env python3
"""Create a read-only copy and checksum inventory of current GPT-5.2 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
DEFAULT_ROOT = ROOT / "runtime" / "gpt52-731" / "snapshots"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(
    destination: Path,
    *,
    output_subdir: str,
    first: int,
    last: int,
) -> dict:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite snapshot: {destination}")
    destination.mkdir(parents=True)
    inventory: list[dict] = []
    totals = {"cases": last - first + 1, "output_dirs": 0, "files": 0, "bytes": 0}

    for label in range(first, last + 1):
        issue_name = f"swe_issue_{label:03d}"
        issue_dir = WORKDIR / issue_name
        source = issue_dir / output_subdir
        target_issue = destination / "workdir" / issue_name
        metadata = issue_dir / "artifacts" / "instance_metadata.json"
        if metadata.is_file():
            metadata_target = target_issue / "artifacts" / metadata.name
            metadata_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(metadata, metadata_target)
        if not source.is_dir():
            inventory.append({"issue": issue_name, "output_exists": False, "files": []})
            continue

        target = target_issue / output_subdir
        shutil.copytree(source, target, copy_function=shutil.copy2)
        files = []
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(destination).as_posix()
            size = path.stat().st_size
            files.append({"path": relative, "bytes": size, "sha256": sha256(path)})
            totals["files"] += 1
            totals["bytes"] += size
        totals["output_dirs"] += 1
        inventory.append({"issue": issue_name, "output_exists": True, "files": files})

    document = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(ROOT),
        "output_subdir": output_subdir,
        "range": [first, last],
        "totals": totals,
        "cases": inventory,
    }
    (destination / "snapshot_manifest.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-subdir", default="outputs_gpt-5.2")
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=731)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if not 1 <= args.first <= args.last <= 731:
        raise ValueError("expected 1 <= first <= last <= 731")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = args.destination or DEFAULT_ROOT / timestamp
    document = snapshot(
        destination.resolve(),
        output_subdir=args.output_subdir,
        first=args.first,
        last=args.last,
    )
    print(
        json.dumps(
            {"destination": str(destination.resolve()), "totals": document["totals"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
