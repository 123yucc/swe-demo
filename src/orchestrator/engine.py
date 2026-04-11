"""
Orchestrator: drives the full repair pipeline as a single Claude Agent SDK loop.

Pipeline:
  Init -> (Parser) -> EvidenceLoop <-> DeepSearch -> PatchPlanner -> PatchGenerator -> Done

The orchestrator is responsible for:

1. Running the parser to produce the initial EvidenceCards.
2. Driving the evidence-closure gap-filling loop via the deep-search subagent.
3. Once closure is reached, dispatching the patch-planner subagent.
4. Once a plan exists, dispatching the patch-generator subagent.
5. Collecting a git diff of the working repo as model_patch.diff.

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
completion by coordinating three subagents: deep-search, patch-planner, and
patch-generator.

The Agent tool accepts only `description`, `prompt`, and `subagent_type`.
Do not pass any other keys.

═══════════════════════════════════════════════════════════
PIPELINE PHASES
═══════════════════════════════════════════════════════════

Phase 1 — Evidence Closure Loop
  Dispatch the `deep-search` subagent to fill gaps in the four evidence cards
  (Symptom, Constraint, Localization, Structural) until no mandatory field is
  empty.  The orchestrator harness auto-persists every deep-search report's
  structured sections to evidence_cards.json via a PostToolUse hook — you do
  NOT need to manually call mcp__evidence__update_localization after each
  deep-search return.  You MAY still call it when you need to add evidence
  that was not in the deep-search report (for example, reconciling two
  reports).

  When evidence is sufficient, transition to Phase 2.

Phase 2 — Patch Planning
  Dispatch the `patch-planner` subagent exactly once.  It will read the
  evidence cards from shared working memory and call submit_patch_plan with
  a structured edit plan.  When it returns "PATCH_PLAN_SUBMITTED", proceed
  to Phase 3.

Phase 3 — Patch Generation
  Dispatch the `patch-generator` subagent exactly once.  It will read the
  patch plan, read each target file, and apply SEARCH/REPLACE edits via
  the apply_search_replace tool.  When it returns "PATCH_APPLIED" or
  "PATCH_INCOMPLETE", proceed to closure.

═══════════════════════════════════════════════════════════
EVIDENCE CLOSURE — MANDATORY non-empty fields
═══════════════════════════════════════════════════════════

Before leaving Phase 1:

- localization.suspect_entities — at least one file + one function
- localization.exact_code_regions — at least one entry
- localization.call_chain_context — at least one chain if a function is suspect
- structural.must_co_edit_relations — at least one entry if suspects exist

HARD RULES:

1. exact_code_regions must NOT be empty before leaving Phase 1.
2. Do NOT declare closure based on TO-BE items — those describe required
   additions, not existing defects.  Items confirmed absent from the codebase
   belong in constraint.missing_elements_to_implement.
3. FACT-ALIGNMENT: every entry written to the JSON evidence cards MUST be
   grounded in what deep-search actually found, not inferred from requirements.

BLOCKING RULES:

- NEVER output PIPELINE_COMPLETE when localization.exact_code_regions is empty.
- If no deep-search call has happened in this session and any mandatory
    Phase 1 field is empty, you MUST dispatch deep-search first.
- suspect_entities entries without file:line evidence are hypotheses only;
    they cannot satisfy closure by themselves.

═══════════════════════════════════════════════════════════
DISPATCHING DEEP SEARCH — focused context injection
═══════════════════════════════════════════════════════════

When dispatching deep-search, structure the prompt as:

```
## TODO
<specific investigation task>

## Current Evidence Context
<only the fields RELEVANT to this task>

## What to focus on
<which evidence dimensions this task should fill>
```

Do NOT dump the full JSON — the subagent gets overwhelmed.

═══════════════════════════════════════════════════════════
FINAL OUTPUT
═══════════════════════════════════════════════════════════

After Phase 3 completes, output a single line:

PIPELINE_COMPLETE

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


def _extract_section(markdown: str, heading: str) -> str:
    """Return the body of a `## <heading>` section, or empty string."""
    pattern = (
        rf"(?:^|\n)##\s+{re.escape(heading)}\s*\n"
        r"(.*?)"
        r"(?=\n##\s+|\Z)"
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
    """Extract entries from the ## EXACT_LINES fenced code block."""
    pattern = (
        r"(?:^|\n)##\s+EXACT_LINES[^\n]*\n"
        r"```[^\n]*\n"
        r"(.*?)"
        r"\n```"
    )
    m = re.search(pattern, markdown, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    entries: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.lower() == "none":
            continue
        entries.append(line)
    return entries


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
    markdown = ""
    if isinstance(tool_response, dict):
        raw_result = tool_response.get("result", "")
        if isinstance(raw_result, str):
            markdown = raw_result
    elif isinstance(tool_response, str):
        markdown = tool_response

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
        "Begin Phase 1 (evidence closure loop) now. Once closure is reached, "
        "proceed through Phase 2 (patch planning) and Phase 3 (patch "
        "generation), then output PIPELINE_COMPLETE."
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
    if not current.localization.exact_code_regions:
        missing.append("localization.exact_code_regions")
    if not current.localization.suspect_entities:
        missing.append("localization.suspect_entities")
    if not current.localization.call_chain_context:
        missing.append("localization.call_chain_context")
    if not current.structural.must_co_edit_relations:
        missing.append("structural.must_co_edit_relations")
    return missing


async def _drain_response_stream(client: ClaudeSDKClient) -> dict[str, Any]:
    """Consume one SDK response stream with detailed observability logs."""
    stats: dict[str, Any] = {
        "assistant_messages": 0,
        "agent_tool_calls": 0,
        "deep_search_calls": 0,
        "result_messages": 0,
        "saw_pipeline_complete_text": False,
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
                    print(f"[orchestrator/stream] text={preview}", flush=True)
                    if "PIPELINE_COMPLETE" in str(text):
                        stats["saw_pipeline_complete_text"] = True
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
                                f"description={desc} prompt={prompt_preview}",
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
                                f"todos={todo_preview}",
                                flush=True,
                            )
                    if tool_name == "Agent":
                        stats["agent_tool_calls"] += 1
                        if subagent_type == "deep-search":
                            stats["deep_search_calls"] += 1
                elif block_type == "ToolResultBlock":
                    is_error = getattr(block, "is_error", None)
                    content = getattr(block, "content", None)
                    content_preview = " ".join(str(content).split())[:180]
                    print(
                        "[orchestrator/stream] tool_result "
                        f"is_error={is_error} content={content_preview}",
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
                f"subtype={message.subtype} is_error={message.is_error} "
                f"num_turns={message.num_turns} "
                f"total_cost_usd={message.total_cost_usd} "
                f"stop_reason={message.stop_reason} result={result_preview}",
                flush=True,
            )

    print(
        "[orchestrator/stream] summary "
        f"assistant={stats['assistant_messages']} "
        f"agent_calls={stats['agent_tool_calls']} "
        f"deep_search_calls={stats['deep_search_calls']} "
        f"result_messages={stats['result_messages']} "
        f"pipeline_complete_text={stats['saw_pipeline_complete_text']}",
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
            "patch-planner": _PATCH_PLANNER_AGENT_DEF,
            "patch-generator": _PATCH_GENERATOR_AGENT_DEF,
        },
        mcp_servers={"evidence": evidence_mcp, "patch": patch_mcp},
        cwd=str(repo_dir),
        max_turns=30,
        max_budget_usd=10.0,
        permission_mode="acceptEdits",
        env=sdk_env(),
        can_use_tool=_tool_permission_guard,
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[_dummy_pre_tool_use])],
            "PostToolUse": [
                HookMatcher(matcher="Agent", hooks=[_persist_deep_search_findings]),
            ],
        },
    )

    # Step 5 — Drive the pipeline.
    async with ClaudeSDKClient(options=options) as client:
        await client.query(initial_prompt_text)
        first_stats = await _drain_response_stream(client)

        # Host-side safety net: if the model exits without any deep-search call
        # while Phase 1 mandatory evidence is still missing, force one recovery turn.
        missing_after_first = _phase1_missing_fields()
        if first_stats["deep_search_calls"] == 0 and missing_after_first:
            print(
                "[orchestrator] recovery-triggered: no deep-search call detected "
                f"while missing={missing_after_first}",
                flush=True,
            )
            recovery_prompt = (
                "Phase 1 is still incomplete because these mandatory fields are empty: "
                f"{', '.join(missing_after_first)}. "
                "You must dispatch Agent with subagent_type='deep-search' now to collect "
                "exact locations and related structural evidence. Do not output "
                "PIPELINE_COMPLETE before a deep-search return is processed."
            )
            await client.query(recovery_prompt)
            await _drain_response_stream(client)

    # Step 6 — Post-run validation and artifact dumps.
    current_evidence = get_submitted_evidence()
    if current_evidence is not None:
        loc = current_evidence.localization
        struc = current_evidence.structural
        warnings: list[str] = []
        if not loc.exact_code_regions:
            warnings.append("exact_code_regions is empty")
        if not loc.suspect_entities:
            warnings.append("suspect_entities is empty")
        if not loc.call_chain_context:
            warnings.append("call_chain_context is empty")
        if not struc.must_co_edit_relations:
            warnings.append("must_co_edit_relations is empty")
        if warnings:
            for w in warnings:
                print(f"[orchestrator] WARNING: {w}", flush=True)
        else:
            print("[orchestrator] All mandatory evidence fields populated.", flush=True)

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

    # Step 7 — Collect the unified diff of the repo as model_patch.diff.
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
