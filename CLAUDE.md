# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Evidence-Closure-Aware Repair Harness: an automated bug-investigation system that reads issue artifacts (Markdown files), extracts structured evidence, iteratively searches a repository, and produces a closure report with exact file:line defect locations.

## Running the Harness

```bash
pip install -r requirements.txt

python -m src.main <issue_id> <artifacts_dir> <repo_dir>

# Example:
python -m src.main face_recognition_issue_001 \
    workdir/face_recognition_issue_001/artifacts \
    workdir/face_recognition_issue_001/repo
```

**Input artifacts** (4 Markdown files in `artifacts_dir`):
- `problem_statement.md`
- `requirements.md`
- `new_interfaces.md`
- `expected_and_current_behavior.md`

**Outputs** written to `<artifacts_dir>/../evidence/`:
- `evidence_cards.json` �?? structured evidence (Pydantic model �?? JSON)
- `closure_report.md` �?? final Markdown closure report

## API Credentials

Config is loaded from `.env` at project root (no extra deps �?? simple key=value parser in `src/config.py`):

```
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_BASE_URL=https://your-relay.example.com/v1  # optional, for proxy/relay
```

`sdk_env()` in `src/config.py` returns these as a dict injected into every `ClaudeAgentOptions(env=...)` call so all sub-agents use the same relay.

## Architecture

### State Machine

```
Init �?? (Parser) �?? UnderSpecified �?? (Deep Search) �?? Evidence Refining �?? Closed
```

### Components

| Component | File | Role |
|-----------|------|------|
| CLI | `src/main.py` | Validates args, calls `run_orchestrator()` |
| Config | `src/config.py` | Reads `.env`, exposes `sdk_env()` |
| Parser Agent | `src/agents/parser_agent.py` | Reads artifacts, extracts `EvidenceCards` via Claude Agent SDK; calls `mcp__ingestion__submit_extracted_evidence` |
| Deep Search Agent | `src/agents/deep_search_agent.py` | Receives a TODO from orchestrator; uses `Grep`, `Read`, `Glob` for multi-dimensional exploration (call chains, data flow, similar patterns); returns Markdown with `EXACT_LINES` block |
| Orchestrator | `src/orchestrator/engine.py` | Main loop as a Claude Agent SDK agent; delegates to Deep Search via `Agent` tool; persists findings via `mcp__evidence__update_localization` |
| MCP Tools | `src/tools/ingestion_tools.py` | In-process MCP server exposing `submit_extracted_evidence` and `update_localization` |
| Data Models | `src/models/evidence.py`, `src/models/context.py` | Pydantic v2 models for 4 evidence cards + session context |

### Four Evidence Cards (Pydantic models — multi-dimensional)

- **SymptomCard** — `observable_failures` (error messages, stack traces), `repair_targets` (expected behaviour), `regression_expectations` (must-not-break behaviours)
- **ConstraintCard** — `semantic_boundaries` (API contracts), `behavioral_constraints` (assertions/invariants; TO-BE items prefixed `TO-BE:`), `backward_compatibility`, `similar_implementation_patterns` (existing similar APIs as reference)
- **LocalizationCard** — `suspect_entities` (files, classes, functions, variables), `exact_code_regions` (confirmed `path:line` strings), `call_chain_context` (Caller-Callee chains), `dataflow_relevant_uses` (Def-Use relationships)
- **StructuralCard** — `must_co_edit_relations` (if A changes, B must too), `dependency_propagation` (interface/package/config dependency paths)

### Orchestrator: Gap-Filling Loop

The orchestrator acts as an Information Foraging Orchestrator. After every Deep Search return it re-assesses each card for gaps (empty key fields) and dispatches targeted Deep Search TODOs until no evidence is still missing. It does NOT judge relevance — only ensures cards have evidence.

### Hard Closure Rules (enforced in orchestrator prompt + programmatic fallback)

1. `exact_code_regions` must NOT be empty before declaring closure
2. Localization must have at least one concrete file AND function in `suspect_entities`
3. Do NOT close based on `TO-BE:` constraint items (those are requirements, not evidence)
4. Suggested fix must address ONLY what requirements specify
5. **Fact-alignment**: every claim in the closure report must be grounded in what Deep Search actually found — `TO-BE:` items must be described as "absent/not yet implemented", never as "already-implemented dead code"; plain constants must not be inferred as vestigial dynamic logic — no extra enhancements

The orchestrator also has a **programmatic fallback** (`_parse_exact_lines_from_report`) that extracts `EXACT_LINES` from the closure report if the agent never called `update_localization`.

### Claude Agent SDK Integration

- All agents (Parser, Deep Search, Orchestrator) are run via `claude_agent_sdk.query()` with `ClaudeAgentOptions`
- Deep Search is registered as an `AgentDefinition` in the orchestrator's `agents={"deep-search": ...}` dict; the orchestrator invokes it via the `Agent` tool
- SDK docs: `docs/claude_sdk_docs/`
- Phase-by-phase implementation plans: `docs/plan/`

## Key Constraints When Modifying This Code

- Prompts must be written entirely in English
- Do not use mocks in tests �?? use real end-to-end tests (double-path, bidirectional assertions, no mock harnesses)
- Do not add adaptive/dynamic logic not explicitly required by constraints
- All `TodoWrite` usage tracks investigation tasks; mark items done immediately when complete
- The `exact_code_regions` output format must remain `path/to/file.py:LINE` or `path/to/file.py:LINE-LINE`





