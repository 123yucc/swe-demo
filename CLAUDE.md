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
python -m src.main --instance-json workdir/swe_issue_001/instance_metadata.json \
    --repo-dir workdir/swe_issue_001/repo
```

**Output** written to `<output-dir>/` (defaults to `workdir/<instance_id>/evidence/`):
- `evidence_cards.json` — structured evidence
- `model_patch.diff` — git diff patch
- `prediction.json` — SWE-bench eval format (`{instance_id, model_patch}`)

## API Credentials

Config is loaded from `.env` at project root (no extra deps �??? simple key=value parser in `src/config.py`):

```
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_BASE_URL=https://your-relay.example.com/v1  # optional, for proxy/relay
```

`sdk_env()` in `src/config.py` returns these as a dict injected into every `ClaudeAgentOptions(env=...)` call so all sub-agents use the same relay.

## Architecture

### State Machine

```
Init �??? (Parser) �??? UnderSpecified �??? (Deep Search) �??? Evidence Refining �??? Closed
```

### Components

| Component | File | Role |
|-----------|------|------|
| CLI | `src/main.py` | Unified entry point: supports SWE-bench Pro instances, local JSON, and legacy artifacts dir |
| Artifacts | `src/artifacts.py` | Converts SWE-bench `problem_statement` into 4 Markdown artifact files |
| Config | `src/config.py` | Reads `.env`, exposes `sdk_env()` |
| Parser Agent | `src/agents/parser_agent.py` | Reads artifacts, returns `EvidenceCards` via SDK structured output (`output_format`) |
| Deep Search Agent | `src/agents/deep_search_agent.py` | Receives a TODO from orchestrator; uses `Grep`, `Read`, `Glob` for multi-dimensional exploration (call chains, data flow, similar patterns); returns Markdown with `EXACT_LINES` block |
| Orchestrator | `src/orchestrator/engine.py` | Main loop as a Claude Agent SDK agent; delegates to Deep Search via `Agent` tool; persists findings via `mcp__evidence__update_localization` and `mcp__evidence__cache_retrieved_code` |
| MCP Tools | `src/tools/ingestion_tools.py` | In-process MCP server exposing `update_localization` and `cache_retrieved_code` |
| Data Models | `src/models/evidence.py`, `src/models/context.py`, `src/models/memory.py` | Pydantic v2 models for 4 evidence cards, session context, and `SharedWorkingMemory` |

### `src/` File Structure (with annotations)

```text
src/
    __init__.py                      # Package marker for Python imports
    main.py                          # CLI entry: loads SWE-bench Pro instance, runs full pipeline
    artifacts.py                     # Converts SWE-bench instance to 4 Markdown artifact files
    config.py                        # Loads .env and provides sdk_env() for all agents

    agents/
        __init__.py                    # Subpackage marker
        parser_agent.py                # Parses 4 artifact markdown files into structured EvidenceCards
        deep_search_agent.py           # Runs focused repository investigation using Grep/Read/Glob

    models/
        __init__.py                    # Subpackage marker
        evidence.py                    # Definitions of Symptom/Constraint/Localization/Structural cards
        context.py                     # Top-level EvidenceCards and session-oriented context models
        memory.py                      # SharedWorkingMemory model used across orchestrator and sub-agents

    orchestrator/
        __init__.py                    # Subpackage marker
        engine.py                      # Main evidence-closure loop and sub-agent dispatching logic

    tools/
        __init__.py                    # Subpackage marker
        ingestion_tools.py             # Custom MCP tools: update_localization and cache_retrieved_code
```

### Four Evidence Cards（Pydantic 多维证据卡，字段中文说明）

以下四张卡共同构成唯一证据真相源（Source of Truth）。每个字段都应尽量基于可验证事实填写，避免推测。

#### 1) SymptomCard（现象卡）

- `observable_failures`：可观测故障现象。
    记录用户真实看到的问题，例如报错信息、异常类型、堆栈、错误输出、功能失效表现。
- `repair_targets`：修复目标。
    记录“修好后应达到什么行为”，即期望结果与验收目标。
- `regression_expectations`：回归保护项。
    记录修复后不能被破坏的既有正确行为（must-not-break）。

#### 2) ConstraintCard（约束卡）

- `semantic_boundaries`：语义边界。
    记录 API/接口契约、函数签名、类型约束、文档明确要求的边界条件。
- `behavioral_constraints`：行为约束。
    记录不变量、断言、业务规则、输入输出约束。
    若是“未来要新增”的要求，必须使用 `TO-BE:` 前缀标记，避免误当成现状。
- `backward_compatibility`：兼容性要求。
    记录必须保持的向后兼容行为（例如默认参数、旧调用方式不破坏）。
- `similar_implementation_patterns`：相似实现模式。
    记录代码库中可参考的类似实现（同类 API、类似模块模式）。
- `missing_elements_to_implement`：缺失待实现元素。
    记录被证实“规范要求存在但当前代码库中不存在”的类/方法/接口。
    该字段用于防止下游代理误以为这些能力已经存在。

#### 3) LocalizationCard（定位卡）

- `suspect_entities`：可疑实体。
    记录可疑文件、类、函数、变量等定位线索。
- `exact_code_regions`：精确代码区间。
    记录已确认的精确位置，格式必须是 `path/to/file.py:LINE` 或 `path/to/file.py:LINE-LINE`。
- `call_chain_context`：调用链上下文。
    记录 Caller -> Callee 链路，说明问题是如何被触发到该位置的。
- `dataflow_relevant_uses`：数据流相关使用点。
    记录 Def-Use（定义-使用）关系，说明关键变量/配置如何流经系统并影响故障。

#### 4) StructuralCard（结构卡）

- `must_co_edit_relations`：必须联动修改关系。
    记录“如果改 A，必须同步改 B”的关系，避免只改局部导致系统不一致。
- `dependency_propagation`：依赖传播路径。
    记录接口、包、配置、模块之间的依赖传播链路，说明改动影响面。

#### 字段填写原则（建议）

- 优先记录可验证事实（代码检索、行号、调用链），避免纯推断。
- `TO-BE:` 仅表示未来待实现需求，不等于当前已存在实现。
- 若 Deep Search 发现了精确行号但未写入 `exact_code_regions`，视为证据未落盘。
- 四张卡应互相补充：
    现象卡回答“出了什么问题”；
    约束卡回答“修复不能越界”；
    定位卡回答“问题在什么位置”；
    结构卡回答“改动会牵连哪里”。

### Orchestrator: Gap-Filling Loop

The orchestrator acts as an Information Foraging Orchestrator. After every Deep Search return it re-assesses each card for gaps (empty key fields) and dispatches targeted Deep Search TODOs until no evidence is still missing. It does NOT judge relevance �? only ensures cards have evidence.

### Hard Closure Rules (enforced in orchestrator prompt)

1. `exact_code_regions` must NOT be empty before declaring closure
2. Localization must have at least one concrete file AND function in `suspect_entities`
3. Do NOT close based on `TO-BE:` constraint items (those are requirements, not evidence)
4. **Fact-alignment**: every entry written to JSON evidence cards must be grounded in what Deep Search actually found, not inferred from requirements

### SharedWorkingMemory (`src/models/memory.py`)

Global shared context across all agents:
- `issue_context` (str): Original issue artifact text (immutable)
- `evidence_cards` (EvidenceCards): The four evidence cards — sole Source of Truth
- `retrieved_code` (Dict[str, str]): Cached code snippets keyed by `filepath:start-end`
- `action_history` (List[str]): Chronological log of orchestrator actions

The memory is initialized after the Parser completes and injected into the orchestrator's initial prompt via `format_for_prompt()`. The `cache_retrieved_code` MCP tool populates `retrieved_code` during the investigation loop.

### Claude Agent SDK Integration

- Parser Agent uses SDK structured output (`output_format` with `EvidenceCards.model_json_schema()`) to return typed evidence directly
- Deep Search is registered as an `AgentDefinition` in the orchestrator's `agents={"deep-search": ...}` dict; the orchestrator invokes it via the `Agent` tool
- Orchestrator dispatches focused evidence context to Deep Search (not full JSON dump)
- SDK docs: `docs/claude_sdk_docs/`
- Phase-by-phase implementation plans: `docs/plan/`

## Key Constraints When Modifying This Code

- Prompts must be written entirely in English
- Do not use mocks in tests or use real end-to-end tests (double-path, bidirectional assertions, no mock harnesses)
- Do not add adaptive/dynamic logic not explicitly required by constraints
- All `TodoWrite` usage tracks investigation tasks; mark items done immediately when complete
- The `exact_code_regions` output format must remain `path/to/file.py:LINE` or `path/to/file.py:LINE-LINE`
- Constantly prioritize the cleanup of legacy artifacts; after each version iteration, rescan the entire codebase to remove obsolete code.




�����Ļش��û�