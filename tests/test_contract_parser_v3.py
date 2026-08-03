from src.agents.contract_parser import build_requirement_ledger, validate_ledger_coverage
from src.models.context import EvidenceCards
from src.models.evidence import ConstraintCard, LocalizationCard, StructuralCard, SymptomCard


def test_interface_fields_stay_in_one_lossless_contract():
    text = """# Requirements:
- Preserve escaped input\\nwithout truncation
- Update both paths

# New interfaces introduced:
- Type: function
  Name: `load_config`
  Path: src/config/loader.py
  Input: key, default
  Output: value
  Description: Loads a configured value.
"""
    items = build_requirement_ledger(text)
    assert len(items) == 3
    interface = items[-1]
    assert interface.contract_kind == "interface"
    assert "Input: key, default" in interface.text
    assert interface.explicit_paths == ["src/config/loader.py"]
    assert "load_config" in interface.explicit_symbols
    validate_ledger_coverage(text, items)


def test_v2_checkpoint_migrates_without_changing_id_or_verdict():
    cards = EvidenceCards.model_validate({
        "schema_version": "v2",
        "symptom": SymptomCard().model_dump(),
        "constraint": ConstraintCard().model_dump(),
        "localization": LocalizationCard().model_dump(),
        "structural": StructuralCard().model_dump(),
        "requirements": [{
            "id": "req-024", "text": "keep", "origin": "requirements",
            "verdict": "AS_IS_VIOLATED", "evidence_locations": ["a.py:1"],
        }],
    })
    assert cards.schema_version == "v3"
    assert cards.requirements[0].id == "req-024"
    assert cards.requirements[0].verdict == "AS_IS_VIOLATED"
    assert cards.requirements[0].parent_contract_id == "contract-024"


def test_multiple_unbulleted_interfaces_are_separate_contracts():
    text = """New interfaces introduced:
Type: Function
Name: one
Path: src/one.py
Input: x
Output: y
Description: first

Type: Class
Name: Two
Path: src/two.py
Input: none
Output: instance
Description: second
"""
    items = build_requirement_ledger(text)
    assert len(items) == 2
    assert items[0].explicit_paths == ["src/one.py"]
    assert items[1].explicit_paths == ["src/two.py"]
    validate_ledger_coverage(text, items)


def test_unbulleted_type_groups_top_level_interface_field_bullets():
    text = """New interfaces introduced:
Type: Struct
- Name: AuditConfig
- Path: internal/config/audit.go
- Fields:
  - Sinks SinksConfig
  - Buffer BufferConfig
- Description: top-level audit configuration

Type: Function
- Name: NewSink
- Path: internal/server/audit/logfile/logfile.go
- Input: logger, path
- Output: Sink, error
- Description: constructs a logfile sink
"""
    items = build_requirement_ledger(text)
    assert len(items) == 2
    assert items[0].explicit_paths == ["internal/config/audit.go"]
    assert "Fields:" in items[0].text
    assert items[1].explicit_paths == ["internal/server/audit/logfile/logfile.go"]
    assert "Input: logger, path" in items[1].text
    validate_ledger_coverage(text, items)


def test_numbered_type_groups_parameter_bullets():
    text = """New interfaces introduced:
1. Type: Function
Name: WithClientUniqueId
Path: model/request/request.go
Input:
- ctx (context.Context)
- clientUniqueId (string)
Output:
- context.Context

2. Type: Function
Name: HasClientUniqueId
Path: model/request/request.go
Input:
- ctx (context.Context)
Output:
- bool
"""
    items = build_requirement_ledger(text)
    assert len(items) == 2
    assert "clientUniqueId (string)" in items[0].text
    assert items[0].explicit_paths == ["model/request/request.go"]
    validate_ledger_coverage(text, items)


def test_unbulleted_requirement_paragraphs_are_preserved_as_behavior_contracts():
    text = """Requirements:
Generate a per-client UUID in the UI and send it on every request via the X-ND-Client-Unique-Id header.

Add server middleware that reads X-ND-Client-Unique-Id and injects the resolved client unique ID into the request context.

Implement event filtering logic for SSE delivery:

If an event has a clientUniqueId in its sender context, do not deliver it to the subscriber with the same clientUniqueId.

If a username exists in the sender context, deliver only to subscribers with the same username.

New interfaces introduced:
1. Type: Function
Name: WithClientUniqueId
Path: model/request/request.go
Input:
- ctx (context.Context)
- clientUniqueId (string)
Output:
- context.Context

2. Type: Function
Name: ClientUniqueIdFrom
Path: model/request/request.go
Input:
- ctx (context.Context)
Output:
- string
- bool
"""
    items = build_requirement_ledger(text)
    assert len(items) == 6
    assert [item.contract_kind for item in items[:4]] == ["behavior"] * 4
    assert items[0].source_span.text.startswith(
        "Generate a per-client UUID in the UI"
    )
    assert [item.text for item in items[:4]] == [
        "Generate a per-client UUID in the UI and send it on every request via the X-ND-Client-Unique-Id header.",
        "Add server middleware that reads X-ND-Client-Unique-Id and injects the resolved client unique ID into the request context.",
        "If an event has a clientUniqueId in its sender context, do not deliver it to the subscriber with the same clientUniqueId.",
        "If a username exists in the sender context, deliver only to subscribers with the same username.",
    ]
    assert items[-1].explicit_paths == ["model/request/request.go"]
    validate_ledger_coverage(text, items)


def test_unbulleted_group_heading_paragraph_is_not_emitted_as_atomic_requirement():
    text = """Requirements:
Create a parent behavior:

If feature flag is enabled, do X.

If feature flag is disabled, do Y.
"""
    items = build_requirement_ledger(text)
    assert [item.text for item in items] == [
        "If feature flag is enabled, do X.",
        "If feature flag is disabled, do Y.",
    ]
    validate_ledger_coverage(text, items)


def test_literal_escaped_newlines_preserve_original_source_span():
    text = (
        "Requirements:\\n- Keep behavior\\n- Add support\\n\\n"
        "New interfaces introduced:\\nType: Function\\n"
        "Name: load\\nPath: src/config/load.py\\nDescription: loads config"
    )
    items = build_requirement_ledger(text)
    assert len(items) == 3
    assert items[0].source_span.text.startswith("- Keep behavior")
    assert items[-1].source_span.text.startswith("Type: Function")
    assert items[-1].explicit_paths == ["src/config/load.py"]
    assert "\\nName: load\\n" in items[-1].source_span.text
    assert text[items[-1].source_span.start:items[-1].source_span.end] == (
        items[-1].source_span.text
    )
    validate_ledger_coverage(text, items)
