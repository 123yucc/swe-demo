from src.orchestrator.engine import _reconcile_final_patch_outcome


def test_final_artifact_reconcile_preserves_model_infra_failure() -> None:
    outcome, approved = _reconcile_final_patch_outcome(
        "MODEL_INFRA_FAILURE",
        True,
        artifact_ok=False,
        artifact_empty_patch=True,
    )

    assert outcome == "MODEL_INFRA_FAILURE"
    assert approved is False


def test_final_artifact_reconcile_downgrades_empty_patch_without_infra() -> None:
    outcome, approved = _reconcile_final_patch_outcome(
        "PATCH_FAILED",
        True,
        artifact_ok=False,
        artifact_empty_patch=True,
    )

    assert outcome == "NO_EFFECT_PATCH"
    assert approved is False
