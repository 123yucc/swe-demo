"""
Orchestrator: drives the full repair pipeline as a single Claude Agent SDK loop.

State machine:
  Init -> (Parser) -> UnderSpecified <-> EvidenceRefining -> Closed
                                                             -> PatchPlanning -> PatchSuccess / PatchFailed

The orchestrator is responsible for:

1. Running the parser to produce the initial EvidenceCards.
2. Driving evidence collection via deep-search (UnderSpecified state).
3. Delegating closure decisions to the closure-checker subagent (only it
   can transition from EvidenceRefining to Closed).
4. Once closed, dispatching patch-planner then patch-generator.
5. Recording the patch outcome and collecting a git diff.

The two SDK-level robustness guarantees live here:

- `_tool_permission_guard` strips hallucinated `isolation` keys from `Agent`
  tool calls (the SDK Agent schema at python_sdk.md:1933-1938 only declares
  description/prompt/subagent_type).  A dummy `PreToolUse` hook is registered
  alongside `can_use_tool` because the Python streaming transport requires it
  (see user_approvals_and_input.md:190).

- `_persist_deep_search_findings` is a `PostToolUse` hook on the `Agent` tool
  that parses the deep-search subagent's markdown report and writes its
  structured sections directly to the evidence JSON via
  `update_localization.handler(...)`.  This decouples "evidence produced" from
  "evidence persisted" so the orchestrator LLM can no longer forget to call
  the MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
)

from src.agents.closure_checker_agent import CLOSURE_CHECKER_SYSTEM_PROMPT
from src.agents.deep_search_agent import DEEP_SEARCH_SYSTEM_PROMPT
from src.agents.parser_agent import _run_parser_async
from src.agents.patch_generator_agent import PATCH_GENERATOR_SYSTEM_PROMPT
from src.agents.patch_planner_agent import PATCH_PLANNER_SYSTEM_PROMPT
from src.config import sdk_env
from src.tools.ingestion_tools import (
    cache_retrieved_code,
    get_submitted_evidence,
    get_working_memory,
    init_working_memory,
    set_evidence_json_path,
    set_repo_root,
    update_localization,
)
from src.tools.patch_tools import apply_search_replace, submit_patch_plan


# ══════════════════════════════════════════════════════════════════════════
# Orchestrator system prompt
# ══════════════════════════════════════════════════════════════════════════

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are a Repair Harness Orchestrator.  Your job is to drive a bug fix to
completion by coordinating four subagents: deep-search, closure-checker,
patch-planner, and patch-generator.

The Agent tool accepts only `description`, `prompt`, and `subagent_type`.
Do not pass any other keys.

═══════════════════════════════════════════════════════════
STATE MACHINE (strictly enforced)
═══════════════════════════════════════════════════════════

You operate a 5-state machine with the following transitions:

  UnderSpecified ──(deep-search)──> EvidenceRefining
  EvidenceRefining ──(closure-checker: CLOSURE_APPROVED)──> Closed
  EvidenceRefining ──(closure-checker: EVIDENCE_MISSING)──> UnderSpecified
  Closed ──(patch-planner)──> PatchPlanning
  PatchPlanning ──(patch-generator succeeds)──> PatchSuccess
  PatchPlanning ──(patch-generator fails)──> PatchFailed

You start in the UnderSpecified state.

═══════════════════════════════════════════════════════════
STATE DESCRIPTIONS AND ALLOWED ACTIONS
═══════════════════════════════════════════════════════════

### UnderSpecified
  Evidence cards have known gaps.  You MUST dispatch `deep-search` subagent
  to fill them.  After deep-search returns, transition to EvidenceRefining.

  Allowed actions: dispatch deep-search, call mcp__evidence__update_localization
  FORBIDDEN: dispatch closure-checker, patch-planner, or patch-generator

### EvidenceRefining
  Deep-search has returned with new findings.  The harness auto-persists
  deep-search structured sections via a PostToolUse hook.  You MAY also
  manually call mcp__evidence__update_localization to reconcile reports.

  Once you believe evidence might be sufficient, you MUST dispatch
  `closure-checker` to get a verdict.  You CANNOT self-judge closure.

  Allowed actions: call mcp__evidence__update_localization, dispatch closure-checker
  FORBIDDEN: dispatch deep-search (go to UnderSpecified first),
             dispatch patch-planner or patch-generator

### Closed
  The closure-checker has returned CLOSURE_APPROVED.  Evidence gathering
  is complete.  Proceed to dispatch `patch-planner` exactly once.

  Allowed actions: dispatch patch-planner
  FORBIDDEN: dispatch deep-search or closure-checker

### PatchPlanning (after patch-planner returns PATCH_PLAN_SUBMITTED)
  Dispatch `patch-generator` exactly once.

  Allowed actions: dispatch patch-generator
  FORBIDDEN: dispatch deep-search, closure-checker, or patch-planner

### PatchSuccess / PatchFailed
  The patch-generator has returned.  Output the final status line.
  FORBIDDEN: dispatch any subagent

═══════════════════════════════════════════════════════════
CLOSURE-CHECKER PROTOCOL
═══════════════════════════════════════════════════════════

ONLY the closure-checker subagent can decide whether evidence is complete.
When dispatching it, pass the FULL current evidence cards JSON:

```
## Evidence Cards (current state)
<full JSON of all four cards>
```

The closure-checker will return one of:
- "VERDICT: CLOSURE_APPROVED" — transition to Closed state
- "VERDICT: EVIDENCE_MISSING" with specific gaps — transition to
  UnderSpecified state and dispatch deep-search with the suggested tasks

You MUST NOT skip the closure-checker or override its verdict.

═══════════════════════════════════════════════════════════
DISPATCHING DEEP SEARCH — focused context injection
═══════════════════════════════════════════════════════════

When dispatching deep-search (only in UnderSpecified state), structure the
prompt as:

```
## TODO
<specific investigation task>

## Current Evidence Context
<only the fields RELEVANT to this task>

## What to focus on
<which evidence dimensions this task should fill>
```

Do NOT dump the full JSON — the subagent gets overwhelmed.

After deep-search returns, the harness auto-persists its structured
sections to evidence_cards.json.  You do NOT need to manually call
mcp__evidence__update_localization after each deep-search return unless
you need to reconcile findings from multiple reports.

═══════════════════════════════════════════════════════════
HARD RULES
═══════════════════════════════════════════════════════════

1. NEVER dispatch patch-planner or patch-generator before the
   closure-checker has returned CLOSURE_APPROVED.
2. NEVER output PIPELINE_COMPLETE before the patch phase finishes.
3. NEVER self-judge evidence closure — always use closure-checker.
4. FACT-ALIGNMENT: every entry written to JSON evidence cards MUST be
   grounded in what deep-search actually found, not inferred.
5. If closure-checker returns EVIDENCE_MISSING, you MUST go back to
   UnderSpecified and dispatch deep-search for the identified gaps.
   Do NOT re-run closure-checker without new deep-search results.

═══════════════════════════════════════════════════════════
FINAL OUTPUT
═══════════════════════════════════════════════════════════

After the patch-generator completes, output a single line:

PIPELINE_COMPLETE: PATCH_SUCCESS

or if the patch-generator reported failure:

PIPELINE_COMPLETE: PATCH_FAILED

Do not write a Markdown closure report, root-cause analysis, or fix summary.
The JSON evidence cards, patch plan, and applied patch are the only outputs.
"""


# ══════════════════════════════════════════════════════════════════════════
# Deep-search report parser (pure, testable)
# ══════════════════════════════════════════════════════════════════════════

_SECTION_TO_FIELD = {
    "Call Chain Context": "call_chain_context",
    "Dataflow Relevant Uses": "dataflow_relevant_uses",
    "Must Co-Edit Relations": "must_co_edit_relations",
    "Dependency Propagation": "dependency_propagation",
    "Missing Elements to Implement": "missing_elements_to_implement",
}

_EXACT_CODE_REGION_PATTERN = re.compile(
    r"^[^:\n\r]+:\d+(?:-\d+)?$"
)


def _extract_section(markdown: str, heading: str) -> str:
    """Return the body of a `<heading>` section, or empty string.

    Accept heading levels from `#` to `####` so minor formatting drift from
    subagents does not break parsing.
    """
    pattern = (
        rf"(?:^|\n)#{1,4}\s+{re.escape(heading)}\s*\n"
        r"(.*?)"
        r"(?=\n#{1,4}\s+|\Z)"
    )
    m = re.search(pattern, markdown, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_list_items(section_body: str) -> list[str]:
    """Split a section body into non-empty, non-"None" entries.

    Accepts bullet-style (`- `, `* `) and plain lines.
    """
    if not section_body:
        return []
    items: list[str] = []
    for raw in section_body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in ("none", "none.", "(none)", "- none", "* none"):
            continue
        # Strip common bullet prefixes
        line = re.sub(r"^[-*+]\s+", "", line)
        if line:
            items.append(line)
    return items


def _extract_exact_lines_block(markdown: str) -> list[str]:
    """Extract entries from an EXACT_LINES section.

    Prefer fenced block parsing, but fall back to plain section lines to
    tolerate output-format drift from subagents.
    """
    pattern = (
        r"(?:^|\n)#{1,4}\s+EXACT_LINES[^\n]*\n"
        r"```[^\n]*\n"
        r"(.*?)"
        r"\n```"
    )
    m = re.search(pattern, markdown, re.DOTALL)
    if m:
        body = m.group(1)
    else:
        body = _extract_section(markdown, "EXACT_LINES")
        if not body:
            return []
    entries: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.lower() == "none":
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        entries.append(line)
    return entries


def _is_valid_exact_code_region(value: str) -> bool:
    """Validate `path/to/file.py:LINE` or `path/to/file.py:LINE-LINE` format."""
    return bool(_EXACT_CODE_REGION_PATTERN.match(value.strip()))


def _valid_exact_code_regions(regions: list[str]) -> list[str]:
    """Keep only exact_code_regions entries with strict path:line shape."""
    return [r for r in regions if _is_valid_exact_code_region(r)]


def _extract_suspect_entities(markdown: str) -> list[str]:
    """Collect suspect entries from Confirmed Defect Locations + New Suspects."""
    entries: list[str] = []
    for heading in ("Confirmed Defect Locations", "New Suspects"):
        body = _extract_section(markdown, heading)
        entries.extend(_extract_list_items(body))
    return entries


def parse_deep_search_report(markdown: str) -> dict[str, list[str]]:
    """Parse a deep-search markdown report into an update_localization args dict.

    Mirrors the contract in DEEP_SEARCH_SYSTEM_PROMPT's "Required output format"
    section.  Returns a dict keyed by the fields accepted by update_localization.
    """
    if not markdown:
        return {}

    result: dict[str, list[str]] = {
        "exact_code_regions": _extract_exact_lines_block(markdown),
        "suspect_entities": _extract_suspect_entities(markdown),
    }
    for heading, field in _SECTION_TO_FIELD.items():
        items = _extract_list_items(_extract_section(markdown, heading))
        if items:
            result[field] = items

    # Drop empty keys to keep the hook log terse
    return {k: v for k, v in result.items() if v}


def _detect_missing_deep_search_sections(markdown: str) -> list[str]:
    """Report which expected deep-search sections are missing from markdown."""
    expected_headings = [
        "EXACT_LINES",
        "Call Chain Context",
        "Dataflow Relevant Uses",
        "Must Co-Edit Relations",
        "Dependency Propagation",
        "Missing Elements to Implement",
        "Confirmed Defect Locations",
        "New Suspects",
    ]
    missing: list[str] = []
    for heading in expected_headings:
        if heading == "EXACT_LINES":
            if _extract_exact_lines_block(markdown) == []:
                missing.append(heading)
            continue
        if not _extract_section(markdown, heading):
            missing.append(heading)
    return missing


# ══════════════════════════════════════════════════════════════════════════
# SDK hooks and permission guard
# ══════════════════════════════════════════════════════════════════════════

async def _dummy_pre_tool_use(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Required workaround — keeps the streaming transport open so that
    `can_use_tool` callbacks are actually invoked.

    See docs/claude_sdk_docs/guides/user_approvals_and_input.md:190.
    """
    return {"continue_": True}


async def _tool_permission_guard(
    tool_name: str,
    input_data: dict[str, Any],
    context: Any,
) -> PermissionResultAllow:
    """Sanitize hallucinated parameters on the Agent tool.

    The SDK Agent tool schema (python_sdk.md:1933-1938) only declares
    description/prompt/subagent_type.  The orchestrator model sometimes adds
    `isolation: "worktree"` from training memory of the Claude Code CLI
    schema, which causes a rejection loop.  We strip such keys and allow the
    call to proceed with a cleansed input.
    """
    if tool_name != "Agent":
        return PermissionResultAllow(updated_input=input_data)

    allowed_keys = {"description", "prompt", "subagent_type"}
    stripped_keys = [k for k in input_data.keys() if k not in allowed_keys]
    if not stripped_keys:
        return PermissionResultAllow(updated_input=input_data)

    sanitized = {k: v for k, v in input_data.items() if k in allowed_keys}
    print(
        "[permission_guard] stripped unknown Agent keys "
        f"{stripped_keys} (subagent_type="
        f"{input_data.get('subagent_type', '?')})",
        flush=True,
    )
    return PermissionResultAllow(updated_input=sanitized)


def _extract_tool_response_text(tool_response: Any) -> str:
    """Extract text from a PostToolUse hook's tool_response field.

    The SDK's tool_response format for Agent calls is opaque (typed as Any).
    This helper tries known formats in order and falls back to str() so
    substring matching (e.g. for CLOSURE_APPROVED) always works.
    """
    if tool_response is None:
        return ""

    # Format 1: dict with "result" key (documented SDK format)
    if isinstance(tool_response, dict):
        raw_result = tool_response.get("result", "")
        if isinstance(raw_result, str) and raw_result:
            return raw_result
        # Format 2: dict with "content" key (MCP-style content blocks)
        content = tool_response.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            if parts:
                return "\n".join(parts)

    # Format 3: plain string
    if isinstance(tool_response, str):
        return tool_response

    # Format 4: list of content blocks at top level
    if isinstance(tool_response, list):
        parts = []
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            return "\n".join(parts)

    # Fallback: stringify the entire response for substring matching
    fallback = str(tool_response)
    print(
        f"[_extract_tool_response_text] used str() fallback, "
        f"type={type(tool_response).__name__}, preview={fallback[:200]}",
        flush=True,
    )
    return fallback


async def _persist_deep_search_findings(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """PostToolUse hook — parse deep-search output and persist it to JSON.

    Fires after every successful `Agent` tool call.  Only acts when the
    subagent_type is `deep-search`.  Extracts structured sections from the
    returned markdown via `parse_deep_search_report` and writes them through
    the existing `update_localization.handler` coroutine so all path
    normalization, dedup, and contradiction-guard logic is reused.
    """
    if input_data.get("hook_event_name") != "PostToolUse":
        return {}
    if input_data.get("tool_name") != "Agent":
        return {}

    tool_input = input_data.get("tool_input") or {}
    if tool_input.get("subagent_type") != "deep-search":
        return {}

    tool_response = input_data.get("tool_response")
    markdown = _extract_tool_response_text(tool_response)

    if not markdown:
        return {}

    args = parse_deep_search_report(markdown)
    if not args:
        missing_sections = _detect_missing_deep_search_sections(markdown)
        print(
            "[post_tool_use_hook] deep-search report had no parseable sections. "
            f"missing_headings={missing_sections}",
            flush=True,
        )
        return {}

    summary = ", ".join(f"{k}={len(v)}" for k, v in args.items())
    print(f"[post_tool_use_hook] auto-persisting deep-search findings: {summary}", flush=True)

    try:
        await update_localization.handler(args)
    except Exception as exc:  # pragma: no cover — defensive log
        print(f"[post_tool_use_hook] update_localization.handler failed: {exc}", flush=True)

    return {}


# Module-level flag set by the PostToolUse hook when the closure-checker
# returns CLOSURE_APPROVED.  Reset at the start of each orchestrator run.
_closure_checker_approved = False


async def _track_closure_checker_verdict(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """PostToolUse hook — detect CLOSURE_APPROVED from the closure-checker."""
    global _closure_checker_approved

    if input_data.get("hook_event_name") != "PostToolUse":
        return {}
    if input_data.get("tool_name") != "Agent":
        return {}

    tool_input = input_data.get("tool_input") or {}
    if tool_input.get("subagent_type") != "closure-checker":
        return {}

    tool_response = input_data.get("tool_response")
    markdown = _extract_tool_response_text(tool_response)

    if "CLOSURE_APPROVED" in markdown:
        _closure_checker_approved = True
        print("[post_tool_use_hook] closure-checker returned CLOSURE_APPROVED", flush=True)
    elif "EVIDENCE_MISSING" in markdown:
        print("[post_tool_use_hook] closure-checker returned EVIDENCE_MISSING", flush=True)
    else:
        print(
            "[post_tool_use_hook] closure-checker returned unrecognized verdict",
            flush=True,
        )

    return {}


# ══════════════════════════════════════════════════════════════════════════
# Subagent definitions
# ══════════════════════════════════════════════════════════════════════════

_DEEP_SEARCH_AGENT_DEF = AgentDefinition(
    description=(
        "Deep search specialist. Use this agent to investigate a specific "
        "TODO task by searching the repository with Grep, Read, and Glob. "
        "It explores call chains, data flow, and similar patterns."
    ),
    prompt=DEEP_SEARCH_SYSTEM_PROMPT,
    tools=["Grep", "Read", "Glob", "TodoWrite"],
    model="sonnet",
)

_CLOSURE_CHECKER_AGENT_DEF = AgentDefinition(
    description=(
        "Closure checker. Evaluates whether the four evidence cards have "
        "sufficient, fact-grounded content to declare evidence closure. "
        "Returns CLOSURE_APPROVED or EVIDENCE_MISSING with specific gaps."
    ),
    prompt=CLOSURE_CHECKER_SYSTEM_PROMPT,
    tools=[],
    model="sonnet",
)

_PATCH_PLANNER_AGENT_DEF = AgentDefinition(
    description=(
        "Patch planner. Reads the evidence cards and cached code and emits a "
        "structured edit plan via mcp__patch__submit_patch_plan. Does not "
        "write code or touch files."
    ),
    prompt=PATCH_PLANNER_SYSTEM_PROMPT,
    tools=["mcp__patch__submit_patch_plan", "TodoWrite"],
    model="sonnet",
)

_PATCH_GENERATOR_AGENT_DEF = AgentDefinition(
    description=(
        "Patch generator. Reads the stored patch plan and target files, then "
        "applies SEARCH/REPLACE edits via mcp__patch__apply_search_replace."
    ),
    prompt=PATCH_GENERATOR_SYSTEM_PROMPT,
    tools=["Read", "mcp__patch__apply_search_replace", "TodoWrite"],
    model="sonnet",
)


# ══════════════════════════════════════════════════════════════════════════
# Pipeline driver
# ══════════════════════════════════════════════════════════════════════════

def _build_initial_prompt(
    issue_id: str,
    repo_dir: Path,
    memory_text: str,
) -> str:
    return (
        f"Issue ID: {issue_id}\n"
        f"Repository root: {repo_dir}\n\n"
        f"{memory_text}\n\n"
        "You are now in the UnderSpecified state. Begin by dispatching "
        "deep-search to investigate the issue and fill the evidence cards. "
        "After deep-search returns, transition to EvidenceRefining and "
        "dispatch closure-checker. Follow the state machine strictly."
    )


def _collect_git_diff(repo_dir: Path) -> str:
    """Return `git diff` of the working tree, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "diff"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"[orchestrator] git diff failed: {exc}", flush=True)
        return ""
    if result.returncode != 0:
        print(f"[orchestrator] git diff exit={result.returncode}: {result.stderr}", flush=True)
    return result.stdout or ""


def _phase1_missing_fields() -> list[str]:
    """Return mandatory Phase 1 evidence fields that are still empty."""
    current = get_submitted_evidence()
    if current is None:
        return ["evidence_cards"]

    missing: list[str] = []
    valid_regions = _valid_exact_code_regions(current.localization.exact_code_regions)
    if not valid_regions:
        missing.append("localization.exact_code_regions")
    if not current.localization.suspect_entities:
        missing.append("localization.suspect_entities")
    if not current.localization.call_chain_context:
        missing.append("localization.call_chain_context")
    if not current.structural.must_co_edit_relations:
        missing.append("structural.must_co_edit_relations")
    return missing


def _console_safe_text(value: Any) -> str:
    """Best-effort conversion so console logging never raises UnicodeEncodeError."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


async def _drain_response_stream(client: ClaudeSDKClient) -> dict[str, Any]:
    """Consume one SDK response stream with detailed observability logs."""
    stats: dict[str, Any] = {
        "assistant_messages": 0,
        "agent_tool_calls": 0,
        "deep_search_calls": 0,
        "closure_checker_calls": 0,
        "closure_approved": False,
        "patch_planner_calls": 0,
        "patch_generator_calls": 0,
        "result_messages": 0,
        "saw_pipeline_complete_text": False,
        "patch_outcome": None,  # "PATCH_SUCCESS" or "PATCH_FAILED" or None
    }

    async for message in client.receive_response():
        msg_type = type(message).__name__
        print(f"[orchestrator/stream] message={msg_type}", flush=True)

        if isinstance(message, AssistantMessage):
            stats["assistant_messages"] += 1
            for block in message.content:
                block_type = type(block).__name__
                if block_type == "TextBlock":
                    text = getattr(block, "text", "")
                    preview = " ".join(str(text).split())[:220]
                    print(
                        f"[orchestrator/stream] text={_console_safe_text(preview)}",
                        flush=True,
                    )
                    if "PIPELINE_COMPLETE" in str(text):
                        stats["saw_pipeline_complete_text"] = True
                        if "PATCH_SUCCESS" in str(text):
                            stats["patch_outcome"] = "PATCH_SUCCESS"
                        elif "PATCH_FAILED" in str(text):
                            stats["patch_outcome"] = "PATCH_FAILED"
                elif block_type == "ToolUseBlock":
                    tool_name = getattr(block, "name", "?")
                    tool_input = getattr(block, "input", {})
                    subagent_type = ""
                    if isinstance(tool_input, dict):
                        subagent_type = str(tool_input.get("subagent_type", ""))
                    print(
                        "[orchestrator/stream] tool_use "
                        f"name={tool_name} subagent_type={subagent_type}",
                        flush=True,
                    )
                    if isinstance(tool_input, dict):
                        if tool_name == "Agent":
                            desc = str(tool_input.get("description", ""))
                            prompt = str(tool_input.get("prompt", ""))
                            prompt_preview = " ".join(prompt.split())[:240]
                            print(
                                "[orchestrator/stream] tool_input Agent "
                                "description="
                                f"{_console_safe_text(desc)} "
                                f"prompt={_console_safe_text(prompt_preview)}",
                                flush=True,
                            )
                        elif tool_name == "TodoWrite":
                            todos = tool_input.get("todos")
                            try:
                                todo_preview = json.dumps(todos, ensure_ascii=False)
                            except Exception:
                                todo_preview = str(todos)
                            todo_preview = " ".join(todo_preview.split())[:280]
                            print(
                                "[orchestrator/stream] tool_input TodoWrite "
                                f"todos={_console_safe_text(todo_preview)}",
                                flush=True,
                            )
                    if tool_name == "Agent":
                        stats["agent_tool_calls"] += 1
                        if subagent_type == "deep-search":
                            stats["deep_search_calls"] += 1
                        elif subagent_type == "closure-checker":
                            stats["closure_checker_calls"] += 1
                        elif subagent_type == "patch-planner":
                            stats["patch_planner_calls"] += 1
                        elif subagent_type == "patch-generator":
                            stats["patch_generator_calls"] += 1
                elif block_type == "ToolResultBlock":
                    is_error = getattr(block, "is_error", None)
                    content = getattr(block, "content", None)
                    content_preview = " ".join(str(content).split())[:180]
                    print(
                        "[orchestrator/stream] tool_result "
                        "is_error="
                        f"{_console_safe_text(is_error)} "
                        f"content={_console_safe_text(content_preview)}",
                        flush=True,
                    )
        elif isinstance(message, SystemMessage):
            print(
                "[orchestrator/stream] system "
                f"subtype={message.subtype}",
                flush=True,
            )
        elif isinstance(message, ResultMessage):
            stats["result_messages"] += 1
            result_preview = " ".join(str(message.result or "").split())[:220]
            print(
                "[orchestrator/stream] result "
                f"subtype={_console_safe_text(message.subtype)} "
                f"is_error={_console_safe_text(message.is_error)} "
                f"num_turns={message.num_turns} "
                f"total_cost_usd={message.total_cost_usd} "
                "stop_reason="
                f"{_console_safe_text(message.stop_reason)} "
                f"result={_console_safe_text(result_preview)}",
                flush=True,
            )

    # Sync the host-side closure flag into stats for the caller.
    stats["closure_approved"] = _closure_checker_approved

    print(
        "[orchestrator/stream] summary "
        f"assistant={stats['assistant_messages']} "
        f"agent_calls={stats['agent_tool_calls']} "
        f"deep_search={stats['deep_search_calls']} "
        f"closure_checker={stats['closure_checker_calls']} "
        f"closure_approved={stats['closure_approved']} "
        f"patch_planner={stats['patch_planner_calls']} "
        f"patch_generator={stats['patch_generator_calls']} "
        f"result_messages={stats['result_messages']} "
        f"pipeline_complete={stats['saw_pipeline_complete_text']} "
        f"patch_outcome={stats['patch_outcome']}",
        flush=True,
    )
    return stats


async def _run_orchestrator_async(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
) -> Path:
    repo_dir = Path(repo_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1 — Parser produces the initial EvidenceCards.
    print("[orchestrator] Running parser agent...", flush=True)
    evidence = await _run_parser_async(artifact_text)
    print("[orchestrator] Parser done.", flush=True)

    # Step 2 — Initialize shared state.
    set_repo_root(repo_dir)
    memory = init_working_memory(issue_context=artifact_text, evidence=evidence)
    memory.record_action("Parser completed — initial evidence cards created.")

    evidence_path = output_dir / "evidence_cards.json"
    evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    print(f"[orchestrator] Evidence cards saved -> {evidence_path}", flush=True)
    set_evidence_json_path(evidence_path.resolve())

    # Step 3 — Build initial prompt with shared memory snapshot.
    initial_prompt_text = _build_initial_prompt(
        issue_id=issue_id,
        repo_dir=repo_dir,
        memory_text=memory.format_for_prompt(),
    )

    # Step 4 — Wire MCP servers and agent options.
    evidence_mcp = create_sdk_mcp_server(
        name="evidence",
        version="1.0.0",
        tools=[update_localization, cache_retrieved_code],
    )
    patch_mcp = create_sdk_mcp_server(
        name="patch",
        version="1.0.0",
        tools=[submit_patch_plan, apply_search_replace],
    )

    options = ClaudeAgentOptions(
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        allowed_tools=[
            "Agent",
            "TodoWrite",
            "mcp__evidence__update_localization",
            "mcp__evidence__cache_retrieved_code",
            "mcp__patch__submit_patch_plan",
            "mcp__patch__apply_search_replace",
        ],
        agents={
            "deep-search": _DEEP_SEARCH_AGENT_DEF,
            "closure-checker": _CLOSURE_CHECKER_AGENT_DEF,
            "patch-planner": _PATCH_PLANNER_AGENT_DEF,
            "patch-generator": _PATCH_GENERATOR_AGENT_DEF,
        },
        mcp_servers={"evidence": evidence_mcp, "patch": patch_mcp},
        cwd=str(repo_dir),
        max_turns=30,
        max_budget_usd=15.0,
        permission_mode="acceptEdits",
        env=sdk_env(),
        can_use_tool=_tool_permission_guard,
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[_dummy_pre_tool_use])],
            "PostToolUse": [
                HookMatcher(
                    matcher="Agent",
                    hooks=[
                        _persist_deep_search_findings,
                        _track_closure_checker_verdict,
                    ],
                ),
            ],
        },
    )

    # Step 5 — Drive the pipeline with host-side state enforcement.
    global _closure_checker_approved
    _closure_checker_approved = False

    async with ClaudeSDKClient(options=options) as client:
        await client.query(initial_prompt_text)
        first_stats = await _drain_response_stream(client)

        # ── Host-side safety net 1: no deep-search while evidence missing ──
        missing_after_first = _phase1_missing_fields()
        if first_stats["deep_search_calls"] == 0 and missing_after_first:
            print(
                "[orchestrator] recovery: no deep-search call "
                f"while missing={missing_after_first}",
                flush=True,
            )
            recovery_prompt = (
                "You are in the UnderSpecified state. These mandatory fields are empty: "
                f"{', '.join(missing_after_first)}. "
                "You MUST dispatch deep-search now to collect evidence. "
                "After deep-search returns, dispatch closure-checker. "
                "Do NOT skip to patch phases."
            )
            await client.query(recovery_prompt)
            second_stats = await _drain_response_stream(client)
            missing_after_recovery = _phase1_missing_fields()
            if second_stats["deep_search_calls"] == 0 and missing_after_recovery:
                raise RuntimeError(
                    "Orchestrator recovery failed: no deep-search call "
                    f"while mandatory fields remain missing: {missing_after_recovery}"
                )

        # ── Host-side safety net 2: closure-checker never called ──
        if not _closure_checker_approved and not first_stats.get("saw_pipeline_complete_text"):
            missing = _phase1_missing_fields()
            if not missing:
                # Evidence looks complete but closure-checker wasn't called.
                print(
                    "[orchestrator] recovery: evidence fields populated but "
                    "closure-checker never returned CLOSURE_APPROVED. "
                    "Forcing closure-checker dispatch.",
                    flush=True,
                )
                current_ev = get_submitted_evidence()
                ev_json = current_ev.model_dump_json(indent=2) if current_ev else "{}"
                closure_prompt = (
                    "You are in the EvidenceRefining state. Evidence fields appear "
                    "populated but the closure-checker has not been called yet. "
                    "You MUST dispatch closure-checker now with the current evidence:\n\n"
                    f"## Evidence Cards (current state)\n```json\n{ev_json}\n```\n\n"
                    "Based on the verdict, either proceed to Closed state (patch "
                    "phases) or return to UnderSpecified for more deep-search."
                )
                await client.query(closure_prompt)
                await _drain_response_stream(client)

        # ── Host-side safety net 3: patch ran without closure approval ──
        if (
            first_stats.get("saw_pipeline_complete_text")
            and not _closure_checker_approved
        ):
            print(
                "[orchestrator] WARNING: pipeline completed but closure-checker "
                "never approved. This violates the state machine.",
                flush=True,
            )

    # Step 6 — Post-run validation (closure-checker is the single gate).
    if not _closure_checker_approved:
        raise RuntimeError(
            "Pipeline failed: closure-checker never returned CLOSURE_APPROVED. "
            "Evidence completeness was not verified."
        )
    print("[orchestrator] Closure-checker approved: True", flush=True)

    current_evidence = get_submitted_evidence()
    if current_evidence is not None:
        evidence_path.resolve().write_text(
            current_evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"[orchestrator] Final evidence JSON saved -> {evidence_path}", flush=True)
    else:
        print("[orchestrator] ERROR: No evidence cards in memory after pipeline.", flush=True)

    wm = get_working_memory()
    if wm is not None:
        wm_path = output_dir / "working_memory.json"
        wm_path.write_text(wm.model_dump_json(indent=2), encoding="utf-8")
        print(
            f"[orchestrator] Working memory saved -> {wm_path} "
            f"({len(wm.retrieved_code)} cached snippets, "
            f"{len(wm.action_history)} actions)",
            flush=True,
        )
        if wm.patch_plan is not None:
            plan_path = output_dir / "patch_plan.json"
            plan_path.write_text(wm.patch_plan.model_dump_json(indent=2), encoding="utf-8")
            print(f"[orchestrator] Patch plan saved -> {plan_path}", flush=True)

    # Step 7 — Record patch outcome for long-term memory.
    patch_outcome_path = output_dir / "patch_outcome.json"
    patch_outcome_data = {
        "issue_id": issue_id,
        "closure_checker_approved": _closure_checker_approved,
        "patch_outcome": first_stats.get("patch_outcome"),
    }
    patch_outcome_path.write_text(
        json.dumps(patch_outcome_data, indent=2), encoding="utf-8"
    )
    print(f"[orchestrator] Patch outcome saved -> {patch_outcome_path}", flush=True)

    # Step 8 — Collect the unified diff of the repo as model_patch.diff.
    diff_text = _collect_git_diff(repo_dir)
    patch_path = output_dir / "model_patch.diff"
    patch_path.write_text(diff_text, encoding="utf-8")
    if diff_text:
        print(f"[orchestrator] model_patch.diff saved -> {patch_path} ({len(diff_text)} bytes)", flush=True)
    else:
        print(f"[orchestrator] WARNING: empty model_patch.diff -> {patch_path}", flush=True)

    return evidence_path


def run_orchestrator(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
) -> Path:
    """Synchronous entry-point. Returns the path to evidence_cards.json."""
    return asyncio.run(
        _run_orchestrator_async(
            issue_id=issue_id,
            repo_dir=repo_dir,
            artifact_text=artifact_text,
            output_dir=output_dir,
        )
    )
