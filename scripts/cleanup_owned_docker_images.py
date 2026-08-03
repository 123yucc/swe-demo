#!/usr/bin/env python3
"""Remove only Docker images recorded in a batch-owned image ledger."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def load_images(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(value, dict):
        return set()
    return {
        image
        for image in value.get("images", [])
        if isinstance(image, str) and image.strip()
    }


def write_ledger(path: Path, images: set[str], removed: list[str], failed: list[str]) -> None:
    value = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "images": sorted(images),
        "last_cleanup": {"removed": removed, "failed": failed},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def cleanup(path: Path) -> tuple[list[str], list[str]]:
    images = load_images(path)
    removed: list[str] = []
    failed: list[str] = []
    for image in sorted(images):
        result = subprocess.run(
            ["docker", "rmi", "-f", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            removed.append(image)
        else:
            failed.append(image)
    write_ledger(path, images - set(removed), removed, failed)
    return removed, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    removed, failed = cleanup(args.ledger.resolve())
    print(json.dumps({"removed": removed, "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
