import asyncio

from src.agents import parser_agent
from src.models.evidence import SymptomCard


def test_parser_model_only_generates_model_owned_fields(monkeypatch):
    captured = {}

    async def fake_query(**kwargs):
        captured.update(kwargs)
        return parser_agent._ParserOutput(
            symptom=SymptomCard(
                observable_failures=["The command exits with an error."],
                repair_targets=["The command succeeds."],
            ),
            constraint=parser_agent._ParserConstraint(
                missing_elements_to_implement=["load_config(key, default) -> value"],
            ),
        )

    monkeypatch.setattr(parser_agent, "run_structured_query", fake_query)
    issue = """Requirements:
- Preserve existing values.

New interfaces introduced:
Type: Function
Name: load_config
Path: src/config/loader.py
Input: key, default
Output: value
"""

    evidence = asyncio.run(parser_agent._run_parser_async(issue))

    assert captured["response_model"] is parser_agent._ParserOutput
    assert "Do not reproduce the Requirements section" in captured["system_prompt"]
    assert evidence.symptom.repair_targets == ["The command succeeds."]
    assert evidence.constraint.missing_elements_to_implement == [
        "load_config(key, default) -> value"
    ]
    assert [item.origin for item in evidence.requirements] == [
        "requirements",
        "new_interfaces",
    ]
    assert evidence.requirements[1].explicit_paths == ["src/config/loader.py"]
    assert evidence.localization.suspect_entities == []
    assert evidence.structural.must_co_edit_relations == []
    assert evidence.schema_version == "v3"
