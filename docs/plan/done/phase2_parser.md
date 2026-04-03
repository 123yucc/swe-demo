# Phase 2: Parser Sub-agent 开发 (基于 Claude Agent SDK)

## 目标
实现 `ParserAgent`。摒弃底层 API 手动调用，将其作为 Claude Agent SDK 中的一个标准 Agent 实例。利用 SDK 的 Tool 机制，将非结构化的 Markdown 文本转化为结构化的 `EvidenceCards` 对象。

## 技术栈
- Claude Agent SDK
- Phase 1 中定义的 `models/evidence.py`

## 开发任务

### 1. 定义数据摄取工具 (`src/tools/ingestion_tools.py`)
在 SDK 中强制输出结构化数据的一个最佳实践是提供一个“提交通具”。
- 编写一个带有 `@tool` 装饰器的函数，例如 `submit_extracted_evidence(evidence: EvidenceCards) -> str`。
- 这个工具在被 Agent 调用时，其内部逻辑其实非常简单：就是将传入的 Pydantic 对象保存到内存或直接返回，以便外层的主程序可以捕获它。
- 必须利用 Pydantic 的类型提示，SDK 会自动将其转化为 Agent 可以理解的 Schema。

### 2. 配置 Parser Sub-agent (`src/agents/parser_agent.py`)
- 实例化一个 Claude SDK 的 `Agent` 对象，命名为 `parser_agent`。
- **挂载工具**：将上一步的 `submit_extracted_evidence` 挂载给它。
- **编写 System Prompt（英文）**：
  - 设定角色：“你是一个极其严谨的软件缺陷分析师。”
  - 核心指令：“你将收到 4 个需求文档。你的唯一任务是提取信息，并**必须调用 `submit_extracted_evidence` 工具**将提取结果提交给我。如果文档中未明确指出具体的代码行号，LocalizationCard 对应字段必须留空，绝不猜测。”

### 3. 实现执行逻辑封装
创建一个简单的包装函数 `run_parser(md_contents: str) -> EvidenceCards`:
- 将拼接好的 MD 文本发给 `parser_agent`。
- 捕获 agent 运行后 `submit_extracted_evidence` 工具接收到的参数。
- 将验证后的 `EvidenceCards` 对象返回给 Orchestrator。

## 验收标准
- 从workdir\face_recognition_issue_001\artifacts下提取4个.md文件。
- 运行封装好的 `run_parser()` 函数。
- 确认 SDK 成功拦截了工具调用，并完美返回了实例化好的 `EvidenceCards` Pydantic 对象。没有复杂的 JSON 解析，一切都在 SDK 层面自动完成。