# Phase 10: Patch Planning Implementation

## Objective
Implement the `PatchPlannerAgent` as an orchestrator subagent to analyze the four `EvidenceCards` in `SharedWorkingMemory` and output a structured, multi-file edit plan (`PatchPlan`). Current implementation returns the plan via SDK structured output and stores it directly in `SharedWorkingMemory.patch_plan`.

## Architectural Constraints
- Prompts must be entirely in English.
- Patch Planner returns a Pydantic-validated `PatchPlan` via SDK structured output.
- The orchestrator stores the returned plan in `SharedWorkingMemory.patch_plan`.
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

### Task 3: Return Structured PatchPlan
**File:** `src/agents/patch_planner_agent.py`
- Call the SDK with `response_model=PatchPlan`.
- Validate the returned plan through Pydantic.
- Store the plan in `SharedWorkingMemory.patch_plan`.

### Task 4: Create Patch Planner Agent Definition
**File:** `src/agents/patch_planner_agent.py`
- Export `PATCH_PLANNER_SYSTEM_PROMPT` (English) and a factory/constant for the `AgentDefinition`.
- **System Prompt Requirements:**
  - Instruct the agent to act as a Senior Staff Engineer planning a bug fix.
  - Mandate that it MUST review `ConstraintCard.behavioral_constraints` and `ConstraintCard.backward_compatibility`.
  - Mandate that it MUST review `StructuralCard.must_co_edit_relations` to populate `co_edit_dependencies`.
  - Instruct it to return a complete structured `PatchPlan`.
  - Do not output code replacements, only the strategic blueprint.
- Tools: no patch-planning MCP tool is required.

### Task 5: Orchestrator Integration
**File:** `src/orchestrator/engine.py`
- Register `patch-planner` in the orchestrator's `agents` dict.
- Call the patch planner directly and persist its returned `PatchPlan`.
- Extend orchestrator system prompt: after evidence closure, transition to `PatchPlanning` state and dispatch the patch-planner subagent.
