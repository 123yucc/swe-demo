from __future__ import annotations

from src.output_paths import model_output_dir_name


def test_model_output_dir_name_preserves_model_version():
    assert model_output_dir_name("gpt-5.2") == "outputs_gpt-5.2"


def test_model_output_dir_name_sanitizes_spaces_and_punctuation():
    assert model_output_dir_name("Claude Sonnet 4.5!") == "outputs_claude-sonnet-4.5"
