import pytest

from scripts.prepare_deep_search_experiment import FIXED_CASES, SMOKE_CASES, prepare


def test_prepare_keeps_fixed_order_and_injects_ablation_env():
    template = {"models": [{"name": "gpt", "env": {"KEEP": "yes"}}], "issues": FIXED_CASES}
    result = prepare(template, SMOKE_CASES, {"DEEP_SEARCH_REFLECTION_MODE": "none"})
    assert result["issues"] == SMOKE_CASES
    assert result["models"][0]["env"] == {"KEEP": "yes", "DEEP_SEARCH_REFLECTION_MODE": "none"}


def test_prepare_refuses_incomplete_template():
    with pytest.raises(ValueError, match="076"):
        prepare({"models": [], "issues": ["021"]}, SMOKE_CASES, {})
