# Phase 13: Fix `isolation: worktree` Rejection Loop and Missing `update_localization` Calls

## Background

When running the full pipeline from `src/main.py` on `workdir/swe_issue_001`, two
systemic defects surfaced in the orchestrator loop that prevent evidence closure
and waste SDK tokens:

1. **`isolation: worktree` rejection loop.** The orchestrator model repeatedly
   dispatches `deep-search` via the `Agent` tool with `isolation: "worktree"` as
   an extra parameter. The current permission guard rejects the call outright,
   the model retries with the same parameter, and the loop never converges. The
   soft rule in `ORCHESTRATOR_SYSTEM_PROMPT` ("NEVER set isolation on Agent
   calls") is insufficient because the SDK's native `Agent` tool exposes
   `isolation` as a first-class parameter, so the model re-adds it by reflex.
2. **`update_localization` never called.** Even when Deep Search returns a
   well-formed report containing an `EXACT_LINES` block, the orchestrator
   frequently skips the mandated `mcp__evidence__update_localization` call and
   either declares closure prematurely or starts another unrelated Deep Search.
   Because the evidence cards on disk stay empty, downstream `patch-planner` and
   `patch-generator` have nothing to work with. Prompt-level "MANDATORY" wording
   is not enough: the model is free to be lazy, and the permission rejection
   loop from (1) often knocks the state machine off the "persist after search"
   path entirely.

## Root Cause Analysis

### Problem 1 — `isolation` parameter cannot be sanitized

`src/orchestrator/engine.py` is the orchestrator driver and currently wires a
`can_use_tool` callback that tries to strip `isolation` from the `Agent` tool
input before returning `PermissionResultAllow(updated_input=...)`. Per the SDK
streaming semantics documented in
[user_approvals_and_input.md](../claude_sdk_docs/guides/user_approvals_and_input.md#L190),
`can_use_tool` only works reliably when a **dummy `PreToolUse` hook that returns
`{"continue_": True}`** is also registered — otherwise the streaming transport
closes before the permission callback is invoked and the SDK falls back to
rejecting the tool call. Our `ClaudeAgentOptions(...)` wires `can_use_tool=...`
but does **not** register the required dummy hook, so every attempt to sanitize
`isolation` silently fails.

Note on `isolation` provenance: the SDK `Agent` tool schema documented at
[python_sdk.md:1933-1938](../claude_sdk_docs/sdk_references/python_sdk.md#L1933-L1938)
only declares `description`, `prompt`, and `subagent_type`; `isolation` is not
a listed field. The orchestrator model injects it from training memory of the
Claude Code CLI schema. The guard therefore sanitizes hallucinated params — it
is not working around a real SDK option.

### Problem 2 — `update_localization` is only soft-enforced

`src/tools/ingestion_tools.py::update_localization` is a pure MCP tool. Its
invocation is driven by prompt instructions in `ORCHESTRATOR_SYSTEM_PROMPT`. The
Claude Agent SDK's `AgentDefinition` (the type used for `deep-search`) does
**not** support `output_format`, so we cannot force a structured return from the
subagent (this was already noted in `docs/plan/done/phase10_patch_planning.md`).

The SDK does, however, support `PostToolUse` hooks (see
[python_sdk.md §PostToolUseHookInput](../claude_sdk_docs/sdk_references/python_sdk.md#L1593)),
and that hook receives the full `tool_response` payload from the completed tool
call — including the markdown report returned by `Agent(deep-search)`. This
gives us a deterministic seam to parse the EXACT_LINES / Call Chain / Co-Edit /
Missing Elements sections out of the subagent report and push them straight
into `SharedWorkingMemory` without relying on the orchestrator LLM to make a
second tool call.

## Goals

1. Eliminate the `isolation: worktree` rejection loop so the orchestrator cannot
   waste tokens retrying the same invalid dispatch.
2. Guarantee that every Deep Search report is persisted into the evidence cards
   regardless of whether the orchestrator LLM remembers to call
   `update_localization`.
3. Keep the change surface small: reuse existing prompt text, existing MCP tool
   validation logic, and existing evidence card models.

## Non-Goals

- Do not redesign the evidence loop state machine.
- Do not touch `patch_planner_agent.py` / `patch_generator_agent.py` behavior.
- Do not introduce mocks or shim tests — follow the project rule of real E2E
  runs only.

## Plan

### Task 1 — Register the dummy `PreToolUse` hook next to `can_use_tool`

**File:** `src/orchestrator/engine.py`

1. Import `HookMatcher` from `claude_agent_sdk` (and
   `PermissionResultAllow` / `PermissionResultDeny` / `ToolPermissionContext`
   from `claude_agent_sdk.types` as needed).
2. Define a module-level async `_dummy_pre_tool_use` hook that returns
   `{"continue_": True}` — this is the workaround documented at
   [user_approvals_and_input.md:109](../claude_sdk_docs/guides/user_approvals_and_input.md#L109)
   and mandated by the Note at
   [user_approvals_and_input.md:190](../claude_sdk_docs/guides/user_approvals_and_input.md#L190).
3. In `ClaudeAgentOptions(...)` inside `_run_orchestrator_async`, add:

   ```python
   hooks={
       "PreToolUse": [HookMatcher(matcher=None, hooks=[_dummy_pre_tool_use])],
       "PostToolUse": [HookMatcher(matcher="Agent", hooks=[_persist_deep_search_findings])],
   },
   can_use_tool=_tool_permission_guard,
   ```

   The `PreToolUse` hook is required to keep `can_use_tool` live; the
   `PostToolUse` hook is added in Task 3.

### Task 2 — Harden `_tool_permission_guard` to sanitize `isolation`

**File:** `src/orchestrator/engine.py`

1. The permission guard must match on `tool_name == "Agent"` and, when the
   `subagent_type` (or equivalent) refers to `deep-search`, strip any
   `isolation` key (and defensively any other worktree-related key such as
   `worktree_path`) from a shallow copy of `input_data`.
2. Return `PermissionResultAllow(updated_input=sanitized_input)` so the call
   proceeds with the cleansed arguments instead of being rejected.
3. For all other tools, return `PermissionResultAllow(updated_input=input_data)`
   unchanged — do NOT deny unknown tools, because that would re-introduce a new
   flavor of rejection loop.
4. Add a `print("[permission_guard] stripped isolation from deep-search call")`
   log line so we can observe the sanitation happening in the run log and
   confirm the dummy hook wiring actually activates the callback.

### Task 3 — Add a `PostToolUse` hook that auto-persists Deep Search findings

**File:** `src/orchestrator/engine.py` (hook body may live in a new
`src/orchestrator/deep_search_sink.py` if the engine file grows unwieldy)

The hook fires after the orchestrator's `Agent` tool call (i.e. after a
`deep-search` subagent returns). It receives a `PostToolUseHookInput` (see
[python_sdk.md:1593-1616](../claude_sdk_docs/sdk_references/python_sdk.md#L1593-L1616))
whose `tool_response` is the `Agent` tool's output dict, documented at
[python_sdk.md:1941-1949](../claude_sdk_docs/sdk_references/python_sdk.md#L1941-L1949)
as `{result: str, usage, total_cost_usd, duration_ms}`. The markdown report is
in the `result` field.

1. Early-exit unless `tool_name == "Agent"` and `tool_input.get("subagent_type") == "deep-search"`
   (the Agent tool's documented input schema at
   [python_sdk.md:1933-1938](../claude_sdk_docs/sdk_references/python_sdk.md#L1933-L1938)
   guarantees the `subagent_type` key exists).
2. Extract the markdown text via `tool_response.get("result", "")`. Guard
   against non-dict shapes defensively (empty string → hook no-ops).
3. Run a lightweight parser that pulls out each structured block by regex:
   - `## EXACT_LINES` fenced block → `exact_code_regions`
   - `## Call Chain Context` → `call_chain_context` (one chain per non-empty
     line, skipping "None")
   - `## Dataflow Relevant Uses` → `dataflow_relevant_uses`
   - `## Must Co-Edit Relations` → `must_co_edit_relations`
   - `## Dependency Propagation` → `dependency_propagation`
   - `## Missing Elements to Implement` → `missing_elements_to_implement`
   - `## New Suspects` / "Confirmed Defect Locations" → `suspect_entities`
   (Mirror the existing prompt contract in `DEEP_SEARCH_SYSTEM_PROMPT`; this
   keeps parsing and generation symmetric.)
4. Call the existing `update_localization` handler **directly** (Python-level,
   not through the agent loop) with a synthesized `args` dict. The `@tool`
   decorator wraps the function into an `SdkMcpTool` dataclass whose `.handler`
   field exposes the original coroutine verbatim — see
   [python_sdk.md:530-550](../claude_sdk_docs/sdk_references/python_sdk.md#L530-L550).
   The hook therefore calls:

   ```python
   from src.tools.ingestion_tools import update_localization
   await update_localization.handler({
       "exact_code_regions": [...],
       "call_chain_context": [...],
       ...
   })
   ```

   This reuses all existing logic: path normalization via `_normalize_path`,
   dedup via `_dedup_by_location`, contradiction guard, working memory
   mutation, and JSON persistence to `_evidence_json_path`. No new write path
   is introduced.
5. Append one line to `SharedWorkingMemory.action_history`:
   `hook: auto-persisted deep-search findings (regions=N, chains=M, ...)`.
6. Return an empty dict from the hook — we only need its side effect; we do
   not need to block the tool or mutate its response.

### Task 4 — Tighten the orchestrator prompt around the new guarantees

**File:** `src/orchestrator/engine.py` (`ORCHESTRATOR_SYSTEM_PROMPT`)

1. Remove the old "NEVER set isolation" soft-rule paragraph (it is now enforced
   by code) and replace it with a single sentence that matches the documented
   SDK schema at
   [python_sdk.md:1933-1938](../claude_sdk_docs/sdk_references/python_sdk.md#L1933-L1938):
   `The Agent tool accepts only description, prompt, and subagent_type; do not
   pass any other keys.`
2. Reword the "PERSISTING FINDINGS — MANDATORY" section to acknowledge the
   auto-persist hook: the orchestrator is still encouraged to call
   `update_localization` itself when it finds gaps in the auto-parsed output,
   but closure is gated on the JSON contents, not on whether the model made the
   call directly. This removes the behavioral trap where the LLM "forgets" and
   then cannot recover.
3. Keep the hard closure rules (exact_code_regions non-empty etc.) intact — the
   auto-persist hook satisfies them automatically when Deep Search produced
   valid output, so the rules still serve as a correctness gate.

### Task 5 — Expose a small helper for the hook and unit-level sanity check

**File:** `src/orchestrator/deep_search_sink.py` (new, optional split)

1. Factor the markdown parser into a pure function
   `parse_deep_search_report(markdown: str) -> dict[str, list[str]]` so it can
   be exercised from a real repository run without touching the SDK.
2. Add a real end-to-end test under `tests/test_deep_search_sink.py` that:
   - Feeds a fixture markdown string that matches the
     `DEEP_SEARCH_SYSTEM_PROMPT` output contract,
   - Asserts each expected key is present in the returned dict,
   - Asserts at least one `file.py:LINE` entry round-trips through
     `_normalize_path` + `_dedup_by_location` to the on-disk evidence JSON.
   No mocks, no SDK stubs — the test runs `parse_deep_search_report` against a
   canned string and then calls the real `update_localization` coroutine with
   a temporary working memory and tmp-path JSON.

### Task 6 — Re-run `workdir/swe_issue_001` end-to-end

1. From the project root:
   ```bash
   python -m src.main --instance-json workdir/swe_issue_001/instance_metadata.json \
       --repo-dir workdir/swe_issue_001/repo
   ```
2. Verify in the run log:
   - At least one `[permission_guard] stripped isolation from deep-search call`
     line appears (confirms the dummy hook wired `can_use_tool` correctly) OR
     no `isolation` key was ever attempted (both are acceptable outcomes).
   - `[update_localization CALLED]` lines appear after every Deep Search
     return, whether driven by the orchestrator LLM or by the PostToolUse hook.
   - `workdir/swe_issue_001/evidence/evidence_cards.json` has non-empty
     `localization.exact_code_regions`, `localization.suspect_entities`, and
     `structural.must_co_edit_relations`.
3. Confirm downstream `patch-planner` and `patch-generator` subagents now
   receive populated evidence and emit a non-empty `model_patch.diff`.
4. Run the SWE-bench Pro evaluation per `docs/plan/phase12_run_one_issue.md`
   and check `eval_results.json` for a resolved status.

## Acceptance Criteria

- No `isolation`-related rejection appears in a clean `swe_issue_001` run.
- `evidence_cards.json` is populated even if the orchestrator never calls
  `update_localization` directly, because the PostToolUse hook wrote it.
- `model_patch.diff` is non-empty and `prediction.json` is written.
- Existing behavior for non-`Agent` tool calls is unchanged.

## Risk & Rollback

- If the `PostToolUse` hook cannot reliably identify the subagent type from the
  `tool_input` of the `Agent` tool (SDK version dependent), fall back to
  parsing any `Agent` tool response that contains the literal string
  `## EXACT_LINES` — the format is distinctive enough to avoid false positives.
- Rollback is a single commit revert: the new hook and guard code are
  self-contained to `engine.py` (+ optional `deep_search_sink.py`) and the
  prompt edits are localized.

## Out of Scope / Follow-ups

- Considering whether `deep-search` should be upgraded from an
  `AgentDefinition` to a top-level `query()` call so it can use SDK structured
  output and remove the markdown-parsing hop entirely. Track separately once
  this phase stabilizes the run.
