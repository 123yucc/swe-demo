from __future__ import annotations

import json

from scripts import build_gpt52_eval_retry_manifest as builder


def test_build_selects_only_unresolved_cases_with_effective_patch(monkeypatch, tmp_path):
    workdir = tmp_path / "workdir"
    source_manifest = tmp_path / "source.json"
    source_manifest.write_text(
        json.dumps({"models": [{"output_subdir": "outputs_gpt-5.2"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "WORKDIR", workdir)
    monkeypatch.setattr(builder, "SOURCE", source_manifest)
    for label, resolved, patch in (
        (1, False, "diff --git a/a b/a\n"),
        (2, True, "diff --git a/b b/b\n"),
        (3, False, ""),
    ):
        output = workdir / f"swe_issue_{label:03d}" / "outputs_gpt-5.2"
        (output / "eval_result").mkdir(parents=True)
        (output / "eval_result" / "eval_summary.json").write_text(
            json.dumps({"resolved": resolved}), encoding="utf-8"
        )
        (output / "patch.diff").write_text(patch, encoding="utf-8")

    selection = builder.build(1, 4, tmp_path / "selection", "outputs_gpt-5.2")

    assert selection["unresolved_with_effective_patch"] == ["001"]
    assert selection["unresolved_without_effective_patch"] == ["003"]
    assert selection["missing_eval"] == ["004"]
    manifest = json.loads((tmp_path / "selection" / "manifest.json").read_text())
    assert manifest["issues"] == ["001"]
