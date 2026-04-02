# Phase 2: Parser Sub-agent 开发 (Data Extraction)

## 目标
实现一个独立的 Parser Agent，利用 Claude SDK 的工具调用（Tool Use）能力，将 4 个非结构化的 Markdown 需求文档转化为 Phase 1 定义的 `EvidenceCards` Pydantic 对象。

## 技术栈
- `anthropic` (Claude Python SDK)
- `instructor` 或原生的 Claude Tool Use (推荐使用原生，更贴近底层控制)
- Phase 1 中定义的 `models/evidence.py`

## 开发任务

### 1. 编写 Parser System Prompt (`src/agents/prompts/parser_prompt.py`)
设计一个详尽的 System Prompt。
- 设定其角色为资深缺陷分析师。
- 明确指出输入是 4 个 XML tag 包裹的文档（`<problem_statement>`, `<expected_and_current_behavior>`, `<requirements>`, `<new_interfaces>`）。
- 明确**提取规则**（Symptom 对应哪些文档，Constraint 对应哪些等）。
- **硬性约束**：如果文档中没有具体到代码行号或文件名，LocalizationCard 对应字段必须留空，绝对禁止 Agent 猜测代码库结构。

### 2. 工具 Schema 转换 (`src/agents/parser_agent.py`)
- 编写一个辅助函数，将 Pydantic 的 `EvidenceCards` 类转化为 Claude API 接受的 Tool Schema 格式（JSON Schema）。
- 工具命名为 `initialize_evidence_cards`。

### 3. 实现执行逻辑 (`src/agents/parser_agent.py`)
创建一个 `ParserAgent` 类，包含 `run` 方法：
- **输入**: 4 个 MD 文件的纯文本字符串。
- **处理**: 
  1. 将 4 个文件的内容用对应的 XML 标签包裹拼接。
  2. 调用 `anthropic` client。
  3. 使用 `tool_choice: {"type": "tool", "name": "initialize_evidence_cards"}` 强制模型输出结构化数据。
- **输出**: 解析 Claude 返回的 Tool Use 请求中的参数，使用 `EvidenceCards.model_validate_json()` 返回一个 Python 对象。

## 验收标准
- 准备 4 个 mock 的 MD 文件（制造一些模拟的 Bug 场景）。
- 运行 `ParserAgent.run()`。
- 打印返回的 `EvidenceCards` 对象，检查内容是否准确从 MD 文件中映射出来，且未提及的代码路径（Localization）是否为空列表。