from __future__ import annotations

import asyncio

from src.agents import _cost_policy


def test_read_hook_caps_requested_lines(monkeypatch):
    monkeypatch.setenv("HARNESS_READ_MAX_LINES", "120")
    result = asyncio.run(_cost_policy._pre_tool_use(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "large.py", "limit": 900},
            "tool_use_id": "read-1",
        },
        None,
        {},
    ))
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert updated["limit"] == 120
    assert updated["file_path"] == "large.py"


def test_grep_hook_adds_bounded_default(monkeypatch):
    monkeypatch.setenv("HARNESS_GREP_MAX_RESULTS", "75")
    result = asyncio.run(_cost_policy._pre_tool_use(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "needle"},
            "tool_use_id": "grep-1",
        },
        None,
        {},
    ))
    assert result["hookSpecificOutput"]["updatedInput"]["head_limit"] == 75


def test_shared_tool_budget_precedes_legacy_openai_name(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_OUTPUT_MAX_CHARS", "9000")
    monkeypatch.setenv("HARNESS_TOOL_OUTPUT_MAX_CHARS", "12000")
    assert _cost_policy.tool_output_max_chars() == 12000
