## Plan: 修复 Orchestrator 提前退出并对齐 SDK

目标是修复 phase13 后的“Parser 后立即结束、无 subagent 调用”问题，并把每一步都绑定到 Claude Agent SDK 文档校验，确保实现与 SDK 机制一致。

**Steps**
1. Phase A 观测闭环基线（阻塞后续）
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 receive_response 循环建立消息级日志：AssistantMessage/TextBlock/ToolUseBlock/ResultMessage/SystemMessage。
- 同时记录工具调用摘要（tool name、subagent_type、关键输入字段）与最终结果状态（是否 error、turns、cost）。
- 依赖：无。
- SDK 冲突检查：核对 [docs/claude_sdk_docs/sdk_references/python_sdk.md](docs/claude_sdk_docs/sdk_references/python_sdk.md) 中 receive_response 示例与 Message Types，确认消息解析方式与字段名一致。

2. Phase B 权限与 Hook 路径有效性校验（依赖 1）
- 校验并修正 can_use_tool + PreToolUse 配置在运行期是否真正生效，确保 `_tool_permission_guard` 的输入清洗确实被调用。
- 增加 guard 命中日志与未命中日志（只加轻量日志，不改业务语义）。
- SDK 冲突检查：核对 [docs/claude_sdk_docs/guides/user_approvals_and_input.md](docs/claude_sdk_docs/guides/user_approvals_and_input.md) 关于 Python 中 can_use_tool 必须配 PreToolUse keep-alive 的要求，确认 hook 返回结构为 continue_。

3. Phase C 防止“零工具直接完成”早退（依赖 1）
- 在 orchestrator 主流程添加宿主侧状态闸门：若 Phase1 必填证据为空且本轮无 deep-search 工具调用，则禁止进入完成路径并触发一次强制补救 query（带具体 TODO）。
- 闸门依据 JSON 状态，不依赖模型自觉文本。
- SDK 冲突检查：核对 [docs/claude_sdk_docs/how_the_agent_loop_works.md](docs/claude_sdk_docs/how_the_agent_loop_works.md) 的回合语义，确保宿主侧重入 query 不破坏会话状态机。

4. Phase D 深搜结果自动落盘鲁棒化（依赖 1、2）
- 强化 PostToolUse 持久化钩子：当 deep-search 返回体中存在结构段时，必须解析并调用 update_localization.handler；解析为空时输出具体缺失段名。
- 对 parse_deep_search_report 的节标题容错做小幅增强（仅与既有 prompt 合同兼容，不引入新格式）。
- SDK 冲突检查：核对 [docs/claude_sdk_docs/sdk_references/python_sdk.md](docs/claude_sdk_docs/sdk_references/python_sdk.md) 的 PostToolUseHookInput 与 Agent 工具返回结构，保证 tool_response.result 读取路径正确。

5. Phase E Prompt 约束与代码闸门一致化（依赖 3、4）
- 收紧 [src/orchestrator/engine.py](src/orchestrator/engine.py) 中 ORCHESTRATOR_SYSTEM_PROMPT，明确在 exact_code_regions 为空时禁止 PIPELINE_COMPLETE。
- 将“可自动持久化”与“仍可显式调用 update_localization”的职责边界写清，避免双重误解。
- SDK 冲突检查：核对 [docs/claude_sdk_docs/guides/subagents_in_the_sdk.md](docs/claude_sdk_docs/guides/subagents_in_the_sdk.md) 关于 Agent tool/subagent_type 的约束，避免提示词要求超出 SDK schema 的字段。

6. Phase F 端到端验证与回归（依赖 1-5）
- 运行同一命令重放问题场景，验证 run_log 中出现：至少一次 Agent deep-search 调用、至少一次 post_tool_use auto-persist、非空 evidence 关键字段。
- 验证输出产物：[workdir/instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan/evidence/evidence_cards.json](workdir/instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan/evidence/evidence_cards.json) 与 model_patch.diff 非空条件。
- SDK 冲突检查：对照 [docs/claude_sdk_docs/sdk_references/python_sdk.md](docs/claude_sdk_docs/sdk_references/python_sdk.md) 的 max_turns/max_budget_usd 语义，必要时加上保护阈值避免新死循环。

**Relevant files**
- [src/orchestrator/engine.py](src/orchestrator/engine.py) — 主流程、hooks、权限守卫、响应流处理、系统提示。
- [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) — 深搜输出格式合同来源（段标题与机器块）。
- [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) — update_localization.handler 的真实落盘路径与约束。
- [run_observation.md](run_observation.md) — 本次失败症状与复现判据。
- [docs/claude_sdk_docs/sdk_references/python_sdk.md](docs/claude_sdk_docs/sdk_references/python_sdk.md) — 客户端消息流、hooks、Agent tool schema。
- [docs/claude_sdk_docs/guides/user_approvals_and_input.md](docs/claude_sdk_docs/guides/user_approvals_and_input.md) — can_use_tool 与 PreToolUse 组合要求。
- [docs/claude_sdk_docs/how_the_agent_loop_works.md](docs/claude_sdk_docs/how_the_agent_loop_works.md) — 回合/工具调用行为边界。
- [docs/claude_sdk_docs/guides/subagents_in_the_sdk.md](docs/claude_sdk_docs/guides/subagents_in_the_sdk.md) — subagent 调度规范。

**Verification**
1. 单次运行日志必须可见消息类型分发（至少 AssistantMessage + ResultMessage）。
2. 若未发生任何 Agent 调用，宿主闸门必须触发补救 query，而非直接收尾。
3. deep-search 结束后 evidence JSON 必须在同轮内发生变化（至少 exact_code_regions 或 suspect_entities 增加）。
4. 关键证据字段非空后才能进入 patch phase；否则继续 Phase1。
5. 同命令重跑两次，不应再出现“0 tools called + 空 patch”静默成功。

**Decisions**
- 优先顺序采用“先观测再改逻辑”，避免盲改。
- 以宿主代码闸门兜底模型不稳定，不把关键正确性仅放在 prompt 约束上。
- 每一步变更完成后，都执行对应 SDK 文档冲突检查并记录结论。

**Further Considerations**
1. 是否将 deep-search 报告解析器从 engine.py 拆分为独立模块，便于后续做小粒度回归。
2. 是否在 run_log 旁新增 machine-readable trace（jsonl）用于后续问题复盘。