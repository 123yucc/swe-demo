# Phase R4: Orchestrator 改为代码驱动循环

## 目标
将 orchestrator 从"单轮 LLM 负责全部流程"改为"代码 while 循环 + LLM 仅在语义节点介入"。

## 改动
- `engine.py` 新增 `run_pipeline()` 函数，用 while 循环驱动状态机：
  - `UnderSpecified`：调用 LLM 推理证据缺口 → 代码拼装 prompt → 调用 deep-search → 代码解析结果 → 更新状态
  - `EvidenceRefining`：代码先跑 `_phase1_missing_fields()` 机械检查 → 不通过则回 UnderSpecified → 通过则调 closure-checker → 代码解析 verdict → 更新状态
  - `Closed/PatchPlanning`：同理
- 移除 `ORCHESTRATOR_SYSTEM_PROMPT` 中的状态机描述和 HARD RULES（已由代码保证）
- 保留一个精简的 orchestrator prompt，只用于"推理证据缺口"等语义任务
- 移除三个事后 safety net（已不需要）

## 验收
- orchestrator LLM 不再负责状态转移决策
- 流程违规在代码层被阻止，无需事后补救
