from __future__ import annotations

import hashlib
import json

from scripts import snapshot_gpt52_results as snapshotter


def test_snapshot_copies_outputs_and_records_checksums(monkeypatch, tmp_path):
    workdir = tmp_path / "workdir"
    source = workdir / "swe_issue_001" / "outputs_gpt-5.2"
    source.mkdir(parents=True)
    content = b"diff --git a/a b/a\n"
    (source / "patch.diff").write_bytes(content)
    monkeypatch.setattr(snapshotter, "WORKDIR", workdir)

    destination = tmp_path / "snapshot"
    document = snapshotter.snapshot(
        destination,
        output_subdir="outputs_gpt-5.2",
        first=1,
        last=2,
    )

    copied = destination / "workdir" / "swe_issue_001" / "outputs_gpt-5.2" / "patch.diff"
    assert copied.read_bytes() == content
    assert document["totals"]["output_dirs"] == 1
    assert document["cases"][0]["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert json.loads((destination / "snapshot_manifest.json").read_text())["range"] == [1, 2]
