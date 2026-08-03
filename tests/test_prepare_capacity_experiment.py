from scripts.prepare_capacity_experiment import SHARDS, prepare


def test_capacity_shards_are_disjoint_and_cover_fixed20() -> None:
    flat = [case for cases in SHARDS.values() for case in cases]
    assert len(flat) == len(set(flat)) == 20


def test_prepare_preserves_model_and_sets_shard() -> None:
    template = {"models": [{"name": "gpt"}], "issues": ["001"]}
    result = prepare(template, "base6")
    assert result["models"] == [{"name": "gpt"}]
    assert result["issues"] == SHARDS["base6"]
    assert result["max_workers"] == 6
