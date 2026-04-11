# Phase 11: Patch Generator Implementation

## Objective

Implement the `PatchGeneratorAgent` as an orchestrator subagent and an associated MCP tool to apply code changes. The agent reads the `PatchPlan` from `SharedWorkingMemory` and produces `SEARCH/REPLACE` blocks. The MCP tool parses these blocks and modifies the local codebase safely.

## Architectural Constraints

- Prompts must be entirely in English.
- Patch Generator is an `AgentDefinition` subagent of the orchestrator, matching the framework.png state machine design. It inherits MCP tools from the orchestrator's `mcp_servers`.
- Do NOT use line-number-based editing. Use exact string matching (SEARCH/REPLACE).
- No mocks in tests; create real temporary directories and files for testing the patch application.

## Step-by-Step Tasks

### Task 1: Create the Apply Patch MCP Tool

**File:** `src/tools/patch_tools.py` (add to the same file as `submit_patch_plan`)

Implement a tool named `apply_search_replace`.

**Input:** A JSON object with fields:
- `filepath` (str): Path to the target file, relative to repo root.
- `blocks` (str): A string containing one or more SEARCH/REPLACE blocks in the format:

```
<<<<
[exact old code to find]
====
[new code to replace it with]
>>>>
```

**Logic:**

1. Normalize the filepath using existing `_normalize_path` helpers.
2. Resolve the filepath against the repo root (reuse `_repo_root` from `ingestion_tools.py` or accept it via module-level state).
3. Read the target file.
4. For each block, ensure the exact old code exists exactly once in the file. If not, return a descriptive error (e.g., "Code snippet found 0 times or multiple times in {filepath}").
5. Apply replacements sequentially (each block modifies the file content in memory, then write back once at the end).
6. Return success or failure messages.

### Task 2: Create Patch Generator Agent Definition

**File:** `src/agents/patch_generator_agent.py`

Export `PATCH_GENERATOR_SYSTEM_PROMPT` (English) and the `AgentDefinition` configuration.

**System Prompt Requirements:**

- Instruct the agent to execute the PatchPlan file by file.
- Explain the strict `<<<< ==== >>>>` SEARCH/REPLACE syntax.
- Require the SEARCH block to include enough context (like function definitions or surrounding loops) to be unique in the file.
- Instruct it to call `mcp__patch__apply_search_replace` for each file, passing the filepath and blocks.
- Instruct it to use the `Read` tool to read target files before generating SEARCH blocks, ensuring exact match.
- Tools: `["Read", "mcp__patch__apply_search_replace"]`.

### Task 3: Orchestrator Integration

**File:** `src/orchestrator/engine.py`

- Register `patch-generator` in the orchestrator's `agents` dict.
- Add `apply_search_replace` to the `patch` MCP server (same server as `submit_patch_plan`).
- Add `mcp__patch__apply_search_replace` to `allowed_tools`.
- Extend orchestrator system prompt for the `PatchGenerator` phase:
  - After `PatchPlanning` succeeds (patch plan is submitted) -> transition to `PatchGenerator`.
  - Dispatch the `patch-generator` subagent with the current `PatchPlan` and `retrieved_code` context.
  - If the tool fails (e.g., bad search block) -> pass the error back as feedback and retry, up to a max limit.
  - Success -> `Patch Success` state.
  - Exhausted retries -> `Patch Failed` state.
