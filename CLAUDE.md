# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Evidence-Closure-Aware Repair Harness: an automated bug-investigation system that reads issue artifacts (Markdown files), extracts structured evidence, iteratively searches a repository, and produces a closure report with exact file:line defect locations.

## Running the Harness

```bash
pip install -r requirements.txt

# By dataset index (loads from HuggingFace)
python -m src.main --index 0 --repo-dir /app

# By instance_id
python -m src.main --instance-id django__django-16046 --repo-dir /app

# From local instance metadata JSON
python -m src.main --instance-json workdir/swe_issue_001/artifacts/instance_metadata.json \
    --repo-dir workdir/swe_issue_001/repo
```

**Repo Initialization**: The harness automatically resets the repo to a clean `base_commit` state before running (via `git reset --hard`, `git clean -fd`, `git checkout`). This ensures patches are generated against the original buggy code, not previously modified code.

**Output** written to `<output-dir>/` (defaults to `workdir/<issue_name>/outputs/` in `--instance-json` mode, otherwise `workdir/<instance_id>/outputs/`):
- `evidence.json` — structured evidence
- `patch.diff` — git diff patch
- `prediction.json` — SWE-bench eval format (`{instance_id, model_patch}`)
- `patch_outcome.json` — closure-checker approval status and patch result (for long-term memory)

## API Credentials

Config is loaded from `.env` at project root (no extra deps — simple key=value parser in `src/config.py`):

```
ANTHROPIC_API_KEY=sk-...
```

`src/config.py` just loads `.env` into `os.environ`; the Claude Agent SDK
reads `ANTHROPIC_API_KEY` directly. No wrapper helpers.

## Architecture

### State Machine

```
Init -> (Parser) -> UnderSpecified --(deep-search per RequirementItem)--> EvidenceRefining
                        ^                                   |
                        |                          Sufficiency + Correct-attribution
                        |                          gates (code, format-only)
                        |                                   |
                        |                          (closure-checker: factual audit
                        |                           of cited code + Consistency)
                        |                            /            \
              EVIDENCE_MISSING                CLOSURE_APPROVED
                (re-open cited reqs,                   |
                 write rework_context)               Closed
                        |                              |
               (rework budget / exhausted?)     (patch-planner)
                   /            \                     |
                  no            yes            PatchPlanning
                  |              |                    |
                loop           ClosureForcedFail  (patch-generator)
                                (terminal)         /            \
                                            PatchSuccess    PatchFailed
```

- The closure-checker remains the sole approver of `EvidenceRefining -> Closed`.
- `ClosureForcedFail` is the budget-exhaustion escape hatch (no patch phase;
  `patch_outcome = EVIDENCE_INCOMPLETE`).
- No patch generation is allowed before `Closed`.

### Components

| Component | File | Role |
|-----------|------|------|
| CLI | `src/main.py` | Unified entry point: supports SWE-bench Pro instances, local JSON, and legacy artifacts dir |
| Artifacts | `src/artifacts.py` | Converts SWE-bench `problem_statement` into 4 Markdown artifact files |
| Config | `src/config.py` | Loads `.env` into `os.environ` for the SDK |
| Parser Agent | `src/agents/parser_agent.py` | Reads artifacts, returns `EvidenceCards` via SDK structured output |
| Deep Search Agent | `src/agents/deep_search_agent.py` | Receives a TODO from orchestrator; uses `Grep`, `Read`, `Glob` for multi-dimensional exploration; returns `DeepSearchReport` via SDK structured output. **Phase 18.E**: two-round design — Round 1 (primary investigation) + Round 2 (self-reflection checking token traceability, boundary enumeration, verdict consistency) |
| Closure Checker Agent | `src/agents/closure_checker_agent.py` | Manifest-driven audit gate (phase 18.B/C): receives pre-computed `AuditManifest`, executes each `AuditTask` with Grep/Read/Glob; returns `ClosureVerdict` with per-task `AuditResult`. Three semantic check types: verdict_vs_code, findings_anti_hallucination, prescriptive_boundary_self_check |
| Orchestrator | `src/orchestrator/engine.py` | Code-driven while-loop pipeline; calls sub-agents directly at semantic nodes; enforces state transitions via `PipelineState` enum. **Phase 18**: integrates `check_structural_invariants`, `build_audit_manifest`, validates manifest coverage, differentiated rework feedback |
| State Machine | `src/orchestrator/states.py` | `PipelineState` enum (including terminal `CLOSURE_FORCED_FAIL`), `ALLOWED_TRANSITIONS` table, `STATE_ACTIONS` per-state allowed subagent types |
| Guards | `src/orchestrator/guards.py` | Mechanical gates: `check_sufficiency`, `check_correct_attribution`, **Phase 18.A**: `check_structural_invariants` (I1/I2/I3). `DeepSearchBudget` iteration limiter. |
| Audit Builder | `src/orchestrator/audit.py` | **Phase 18.B**: `build_audit_manifest()` produces deterministic `AuditManifest` from evidence. All audit scope decisions are code-driven. |
| Patch Planner Agent | `src/agents/patch_planner_agent.py` | Reads evidence cards, returns `PatchPlan` via SDK structured output. **Phase 18.D**: populates `preserved_findings` per file — verbatim prescriptive snippets from findings that are hard constraints for patch-generator. |
| Patch Generator Agent | `src/agents/patch_generator_agent.py` | Reads PatchPlan and target files, applies SEARCH/REPLACE edits via MCP tool. **Phase 18.D**: respects `preserved_findings` as hard constraints, receives original requirements text in prompt. |
| Evidence MCP Tools | `src/tools/ingestion_tools.py` | In-process MCP server exposing `update_localization` (scope-based replace), `update_requirement_verdict`, and `cache_retrieved_code`; also provides `reset_requirement_for_rework(rid, audit_feedback)` for the phase-17 rework path (clears verdict/locations/findings, stashes audit feedback on `RequirementItem.rework_context`) |
| Patch MCP Tools | `src/tools/patch_tools.py` | In-process MCP server exposing `submit_patch_plan` and `apply_search_replace` |
| Data Models | `src/models/evidence.py`, `context.py`, `memory.py`, `patch.py`, `verdict.py`, `report.py`, `audit.py` | Pydantic v2 models for evidence cards, `ClosureVerdict`, `DeepSearchReport`, `PatchPlan`, **Phase 18.B**: `AuditManifest`, `AuditTask`, `AuditResult` |

### `src/` File Structure (with annotations)

```text
src/
    __init__.py                      # Package marker for Python imports
    main.py                          # CLI entry: loads SWE-bench Pro instance, runs full pipeline
    artifacts.py                     # Converts SWE-bench instance to 4 Markdown artifact files
    config.py                        # Loads .env into os.environ for the SDK

    agents/
        __init__.py                    # Subpackage marker
        parser_agent.py                # Parses 4 artifact markdown files into structured EvidenceCards
        deep_search_agent.py           # Returns DeepSearchReport via SDK structured output
        closure_checker_agent.py       # Returns ClosureVerdict via SDK structured output
        patch_planner_agent.py         # Returns PatchPlan via SDK structured output
        patch_generator_agent.py       # Executes PatchPlan via SEARCH/REPLACE edits

    models/
        __init__.py                    # Subpackage marker
        evidence.py                    # Definitions of Symptom/Constraint/Localization/Structural cards
        context.py                     # Top-level EvidenceCards and session-oriented context models
        memory.py                      # SharedWorkingMemory model used across orchestrator and sub-agents
        patch.py                       # PatchPlan and FileEditPlan models (Phase 18.D: preserved_findings field)
        verdict.py                     # ClosureVerdict model (Phase 18.B: audited list of AuditResult)
        report.py                      # DeepSearchReport model for deep-search structured output
        audit.py                       # AuditManifest, AuditTask, AuditResult (Phase 18.B)

    orchestrator/
        __init__.py                    # Subpackage marker
        engine.py                      # Code-driven while-loop pipeline with direct sub-agent calls
        states.py                      # PipelineState enum, ALLOWED_TRANSITIONS, STATE_ACTIONS
        guards.py                      # Mechanical pre-checks (sufficiency, attribution, structural invariants I1/I2/I3)
        audit.py                       # build_audit_manifest() for deterministic audit scope (phase 18.B)

    tools/
        __init__.py                    # Subpackage marker
        ingestion_tools.py             # Evidence MCP tools: update_localization and cache_retrieved_code
        patch_tools.py                 # Patch MCP tools: submit_patch_plan and apply_search_replace
```

### Evidence Cards + RequirementItem[]

`EvidenceCards` (`schema_version == "v2"`) is the sole Source of Truth.

- **SymptomCard** — `observable_failures`, `repair_targets`, `regression_expectations`
- **ConstraintCard** — `semantic_boundaries`, `behavioral_constraints`, `backward_compatibility`, `similar_implementation_patterns`, `missing_elements_to_implement`
- **LocalizationCard** — `suspect_entities`, `exact_code_regions` (format: `path/to/file.py:LINE` or `path/to/file.py:LINE-LINE`), `call_chain_context`, `dataflow_relevant_uses`
- **StructuralCard** — `must_co_edit_relations`, `dependency_propagation`
- **requirements: list[RequirementItem]** — the task-driving unit. Each item has `id`, `text`, `origin`, `verdict` (`UNCHECKED | AS_IS_COMPLIANT | AS_IS_VIOLATED | TO_BE_MISSING | TO_BE_PARTIAL`), `evidence_locations`, `findings`.

Phase 16 removed the `TO-BE:` prefix convention: TO-BE requirements are first-class `RequirementItem`s, verdict-tracked.

### Field → Writer Ownership

| Field | Writer |
|---|---|
| `symptom.*` | Parser only |
| `constraint.missing_elements_to_implement` | Parser only (from "New interfaces introduced:") |
| `requirements` | Parser initializes (verdict=UNCHECKED); Deep-search updates via `update_requirement_verdict` |
| `localization.*`, `structural.*` | Deep-search (via `update_localization`, scope-keyed by `requirement_id`) |
| `constraint.behavioral_constraints / semantic_boundaries / backward_compatibility / similar_implementation_patterns` | Deep-search (AS-IS observations) |

Ownership is enforced in two places:
- Parser: `_enforce_parser_field_whitelist` clears any deep-search-owned field the parser mistakenly fills.
- `update_localization`: rejects calls touching parser-owned field names (ERROR return value).

### Orchestrator: Code-Driven State-Machine Pipeline

The orchestrator drives a 7-state machine (`UnderSpecified`, `EvidenceRefining`, `Closed`, `PatchPlanning`, `PatchSuccess`, `PatchFailed`, `ClosureForcedFail`) via a **code while-loop**. LLM is only invoked at semantic decision points. All flow control is enforced by code:

- **State transitions** are validated against `ALLOWED_TRANSITIONS` in `states.py`.
- **TODO picker** selects the next `UNCHECKED` RequirementItem; one requirement per deep-search round.
- **Sufficiency gate** (`check_sufficiency`) requires every requirement to have a non-`UNCHECKED` verdict before the closure-checker is consulted.
- **Correct-attribution gate** (`check_correct_attribution`) requires every non-compliant requirement to cite at least one `evidence_location`.
- **Closure-checker** (phase 17) operates as a code-reviewer-style **auditor**: it opens each non-compliant requirement's cited `evidence_locations` via Grep/Read/Glob and judges (a) factual support — does the code actually show the claimed violation, and (b) cross-requirement consistency — when multiple requirements cite overlapping regions, are their verdicts compatible with the code.  AS_IS_COMPLIANT requirements with no overlap are skipped (cost control).
- **Budget exhaustion**: `DeepSearchBudget` (default 5) allows exactly one forced closure-checker pass on exhaustion; any `EVIDENCE_MISSING` result at that point transitions to `CLOSURE_FORCED_FAIL` (patch phase skipped, `patch_outcome = EVIDENCE_INCOMPLETE`).
- **Structured output** for all sub-agents — no regex parsing.
- Findings are persisted via `update_requirement_verdict.handler()` + `update_localization.handler()` from engine code.

### Closure Rules (layered: mechanical gates + structural invariants + LLM audit, phase 18)

**Phase 18 added three layers of deterministic quality gates before closure-checker LLM invocation:**

1. **Sufficiency** (code): every `RequirementItem.verdict != "UNCHECKED"`.
2. **Correct attribution** (code, format-only): every requirement with verdict ≠ `AS_IS_COMPLIANT` has non-empty `evidence_locations`, and every entry matches `path:LINE` or `path:LINE-LINE`.
3. **Structural invariants** (code, phase 18.A):
   - I1: new_interface ↔ missing_elements bidirectional mapping — every `origin=="new_interfaces"` req must have its interface name in `constraint.missing_elements_to_implement`, and every missing_element must correspond to a new_interface req.
   - I2: new_interface cannot be AS_IS_COMPLIANT — by definition a new interface does not exist; any such verdict is deep-search hallucination, reset to UNCHECKED for rework.
   - I3: symptom → requirements coverage — each `symptom.observable_failures` must share ≥2 non-stopword tokens with at least one `requirements`-origin req.

4. **AuditManifest** (code-driven scope, phase 18.B): `build_audit_manifest()` computes which requirements to audit and what checks each needs. Rules:
   - Non-compliant reqs → full checks (verdict_vs_code + findings_anti_hallucination + prescriptive_boundary_self_check if findings contain prescriptive language)
   - `origin==new_interfaces` → always verdict_vs_code + anti_hallucination
   - AS_IS_COMPLIANT with overlapping evidence_locations → verdict_vs_code only
   - Findings with backtick snippets → add findings_anti_hallucination

5. **Closure-checker LLM audit** (phase 18.C): executes the AuditManifest tasks, opens cited code via Grep/Read/Glob, and judges:
   - **verdict_vs_code**: does the code at cited locations actually support the verdict?
   - **findings_anti_hallucination**: are backtick-enclosed snippets in findings verified in the Read output?
   - **prescriptive_boundary_self_check**: if findings contain prescriptive fix language, enumerate ≥2 edge cases and verify the fix satisfies all of them.

6. **Closure-checker gate**: returns `CLOSURE_APPROVED` if all AuditResult checks pass; `EVIDENCE_MISSING` otherwise. The orchestrator validates manifest coverage before accepting the verdict.

7. **Rework path** (phase 18.F): on `EVIDENCE_MISSING`, per-requirement feedback is differentiated by failure type:
   - new_interface_cannot_be_compliant → parser marked new interface, must be TO_BE_MISSING
   - findings_anti_hallucination → findings claimed code that was not verified, delete/rephrase
   - prescriptive_boundary_self_check → prescriptive fix fails edge case, re-verify
   Capped at `rework_rounds_max = 3`; exhaustion → `CLOSURE_FORCED_FAIL`.

### SharedWorkingMemory (`src/models/memory.py`)

Global shared context across all agents:
- `issue_context` (str): Original issue artifact text (immutable)
- `evidence_cards` (EvidenceCards): The four evidence cards — sole Source of Truth
- `retrieved_code` (Dict[str, str]): Cached code snippets keyed by `filepath:start-end`
- `action_history` (List[str]): Chronological log of orchestrator actions

The memory is initialized after the Parser completes and injected into the orchestrator's initial prompt via `format_for_prompt()`. The `cache_retrieved_code` MCP tool populates `retrieved_code` during the investigation loop.

### Claude Agent SDK Integration

- All sub-agents (Parser, Deep Search, Closure Checker, Patch Planner, Patch Generator) use SDK structured output (`output_format`) to return typed results — no free-text + regex parsing
- The orchestrator calls sub-agents directly via `async` functions (`_run_deep_search_async`, `_run_closure_checker_async`, etc.) — not via the `Agent` tool
- Patch Generator still uses `ClaudeSDKClient` with MCP tools for SEARCH/REPLACE edits
- SDK docs: `docs/claude_sdk_docs/`
- Phase-by-phase implementation plans: `docs/plan/`

### SDK Alignment Decision Log

These deliberate deviations from SDK "default" shapes are documented so they are not re-questioned later:

- We do NOT use SDK `agents={}` + `AgentDefinition` + the `Agent` tool for sub-agent dispatch. SDK subagents return only the final assistant-message string, which prevents us from getting Pydantic-validated structured output. We instead open an independent `query()` per sub-agent with `output_format={"type": "json_schema", ...}` so every round yields a strongly typed result.
- `action_history` stores ONLY cross-agent aggregate events. Per-message detail for a single `query()` is already persisted by the SDK under `~/.claude/projects/<encoded-cwd>/*.jsonl`; duplicating that in our memory would waste space and drift.
- `DeepSearchBudget` is pipeline-level (caps total deep-search invocations). SDK's `max_turns` / `max_budget_usd` are single-query-level (caps tool-use rounds and dollar cost within one `query()` call). They are complementary, not redundant.

## Key Constraints When Modifying This Code

- Prompts must be written entirely in English
- Do not use mocks in tests or use real end-to-end tests (double-path, bidirectional assertions, no mock harnesses)
- Do not add adaptive/dynamic logic not explicitly required by constraints
- All `TodoWrite` usage tracks investigation tasks; mark items done immediately when complete
- The `exact_code_regions` output format must remain `path/to/file.py:LINE` or `path/to/file.py:LINE-LINE`
- Constantly prioritize the cleanup of legacy artifacts; after each version iteration, rescan the entire codebase to remove obsolete code.


中文回答用户