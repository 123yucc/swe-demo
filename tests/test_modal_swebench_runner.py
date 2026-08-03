import sys
import types
from pathlib import Path

import pytest

import eval.modal_swebench_runner as runner


def test_require_modal_credentials_rejects_missing_token(monkeypatch) -> None:
    config = types.SimpleNamespace(get=lambda key: None)
    module = types.SimpleNamespace(config=config)
    monkeypatch.setitem(sys.modules, "modal.config", module)

    with pytest.raises(RuntimeError, match="credentials are missing"):
        runner._require_modal_credentials()


def test_require_modal_credentials_accepts_configured_token(monkeypatch) -> None:
    values = {"token_id": "id", "token_secret": "secret"}
    config = types.SimpleNamespace(get=values.get)
    module = types.SimpleNamespace(config=config)
    monkeypatch.setitem(sys.modules, "modal.config", module)

    runner._require_modal_credentials()


def test_missing_eval_outputs_distinguishes_infra_failure_from_unresolved(
    tmp_path: Path,
) -> None:
    completed = tmp_path / "instance-completed"
    completed.mkdir()
    (completed / "_output.json").write_text(
        '{"resolved": false}', encoding="utf-8"
    )

    assert runner._missing_eval_outputs(
        tmp_path, ["instance-completed", "instance-no-output"]
    ) == ["instance-no-output"]
