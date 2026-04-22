# Phase R1: 状态机代码化

## 目标
将状态机从 prompt 文字定义改为代码驱动的 enum + transition table，代码层强制校验转移合法性，LLM 无法绕过。

## 改动
- `src/orchestrator/states.py`：定义 `PipelineState` enum（UnderSpecified, EvidenceRefining, Closed, PatchPlanning, PatchSuccess, PatchFailed）
- 定义 `ALLOWED_TRANSITIONS: dict[PipelineState, set[PipelineState]]` 转移表
- 定义 `STATE_ACTIONS: dict[PipelineState, set[str]]` 每个状态允许的子 agent 类型
- `engine.py` 中维护 `current_state` 变量，PreToolUse hook 检查 Agent 调用的 subagent_type 是否在当前状态允许列表内，不允许则拒绝

## 验收
- 状态转移只能走 transition table 定义的路径
- LLM 在 UnderSpecified 状态调用 patch-planner 会被代码拒绝
