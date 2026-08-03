from pathlib import Path

from src.main import archive_model_calls, clear_terminal_artifacts


def test_clear_terminal_artifacts_archives_instead_of_deleting(tmp_path: Path) -> None:
    patch = tmp_path / "patch.diff"
    metrics = tmp_path / "run_metrics.json"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    metrics.write_text('{"cost": 1}', encoding="utf-8")

    clear_terminal_artifacts(tmp_path)

    assert not patch.exists()
    assert not metrics.exists()
    archives = list((tmp_path / "history" / "terminal_artifacts").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "patch.diff").read_text(encoding="utf-8") == (
        "diff --git a/a b/a\n"
    )
    assert (archives[0] / "run_metrics.json").read_text(encoding="utf-8") == (
        '{"cost": 1}'
    )


def test_archive_model_calls_preserves_prior_metrics(tmp_path: Path) -> None:
    calls = tmp_path / "model_calls.jsonl"
    calls.write_text('{"request": 1}\n', encoding="utf-8")

    archived = archive_model_calls(tmp_path)

    assert archived is not None
    assert not calls.exists()
    assert archived.read_text(encoding="utf-8") == '{"request": 1}\n'
