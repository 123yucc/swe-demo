# Phase 10: Patch Planning Implementation

## Objective
Implement the `PatchPlannerAgent` as an orchestrator subagent to analyze the four `EvidenceCards` in `SharedWorkingMemory` and output a structured, multi-file edit plan (`PatchPlan`). The plan is persisted via an MCP tool (`submit_patch_plan`), consistent with the existing `update_localization` pattern.

## Architectural Constraints
- Prompts must be entirely in English.
- Patch Planner is an `AgentDefinition` subagent of the orchestrator (not a standalone `query()` call), matching the framework.png state machine design.
- Since `AgentDefinition` does not support `output_format`, use an MCP tool to persist the structured plan instead of SDK structured output.
- Do NOT modify the file system in this phase.
- No mocks in tests; use real End-to-End file generation if needed for tests.

## Step-by-Step Tasks

### Task 1: Define Patch Models
**File:** `src/models/patch.py`
- Create Pydantic v2 models:
  - `FileEditPlan`: Fields for `filepath` (str), `target_functions` (List[str]), `change_rationale` (str), and `co_edit_dependencies` (List[str]).
  - `PatchPlan`: Fields for `overview` (str) and `edits` (List[FileEditPlan]).

### Task 2: Update SharedWorkingMemory
**File:** `src/models/memory.py`
- Add an optional `patch_plan` field (`PatchPlan | None = None`) to `SharedWorkingMemory`.
- Update `format_for_prompt()` to include the patch plan when present.

### Task 3: Create `submit_patch_plan` MCP Tool
**File:** `src/tools/patch_tools.py`
- Implement an MCP tool named `submit_patch_plan` using the `@tool` decorator.
- Input schema: a JSON object matching `PatchPlan` structure (overview + list of edits).
- Logic: validate the input, construct a `PatchPlan` model, store it in `SharedWorkingMemory.patch_plan`, and return a confirmation message.
- Reuse the path normalization helpers from `ingestion_tools.py` (e.g. `_normalize_path`) for filepath fields.
- Use module-level state access pattern consistent with `ingestion_tools.py`.

### Task 4: Create Patch Planner Agent Definition
**File:** `src/agents/patch_planner_agent.py`
- Export `PATCH_PLANNER_SYSTEM_PROMPT` (English) and a factory/constant for the `AgentDefinition`.
- **System Prompt Requirements:**
  - Instruct the agent to act as a Senior Staff Engineer planning a bug fix.
  - Mandate that it MUST review `ConstraintCard.behavioral_constraints` and `ConstraintCard.backward_compatibility`.
  - Mandate that it MUST review `StructuralCard.must_co_edit_relations` to populate `co_edit_dependencies`.
  - Instruct it to call `mcp__patch__submit_patch_plan` with the complete plan.
  - Do not output code replacements, only the strategic blueprint.
- Tools: `["mcp__patch__submit_patch_plan"]` (read-only; no file access needed).

### Task 5: Orchestrator Integration
**File:** `src/orchestrator/engine.py`
- Register `patch-planner` in the orchestrator's `agents` dict.
- Add `patch` MCP server (from `patch_tools.py`) to `mcp_servers`.
- Add `mcp__patch__submit_patch_plan` to `allowed_tools`.
- Extend orchestrator system prompt: after evidence closure, transition to `PatchPlanning` state and dispatch the patch-planner subagent.
