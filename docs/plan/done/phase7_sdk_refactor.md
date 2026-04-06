# Phase 7: SDK 深度重构与护栏原生化 (SDK Refactoring & Native Guardrails)

## 目标
将前期验证成功的业务逻辑（高维 EvidenceCards 提取、前置清洗过滤器）全面迁移到 Claude Agent SDK 的高级特性上，消除冗余代码，提升系统的模块化和稳定性。

## 技术栈
- Claude Agent SDK
- Pydantic

## 开发任务
7.1 Parser Agent 结构化输出

parser_agent.py：移除 MCP 工具调用方式，改用 SDK output_format + EvidenceCards.model_json_schema()，从 ResultMessage.structured_output 获取结果并用 Pydantic 校验
ingestion_tools.py：移除 submit_extracted_evidence 工具及相关的 _EVIDENCE_SCHEMA、ingestion_server，新增 set_submitted_evidence() 供 engine 调用


7.2 Deep Search evidence 注入优化

engine.py：在 orchestrator system prompt 中新增 "DISPATCHING DEEP SEARCH" 指引，要求 orchestrator 传递聚焦的 evidence 上下文而非全量 JSON