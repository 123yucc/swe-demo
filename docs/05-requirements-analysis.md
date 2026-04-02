# 需求分析（Agentic Repair Harness）

## 1. 背景与问题定义

本项目目标是构建一个以证据闭环（Evidence Closure）为核心的软件修复 Agent。

核心问题不是“能否生成 patch”，而是“何时有足够证据提交 patch”。

## 2. 产品目标

- 在 patch 前完成四类证据构建：symptom / localization / constraint / structural。
- 通过 closure 判定控制进入 patch 规划。
- 让每个阶段输出可追溯、可回放、可审计。

## 3. 当前实现状态（基于代码）

已实现主能力：

- 单一执行栈：`src/orchestration/`（LLM Orchestrator + 状态机 + worker runtime）。
- Phase 1：`src/workers/phase1_artifact_parser.py` 解析 artifacts 并生成四类 v1 卡片。
- Phase 2：`src/workers/phase2_*` 提取器与 `src/workers/phase2_enhancer.py` 生成 v2 卡片。
- Phase 3-6：`src/closure_checker.py`、`src/patch_planner.py`、`src/patch_executor.py`、`src/validator.py`。
- 主入口：`main.py --dynamic`，由 `src/app/cli.py` 委托到 `src/pipelines/run_repair_workflow.py`。

当前限制：

- Phase1/Phase2 当前为最小可运行实现，策略质量与提取精度仍可持续增强。
- Orchestrator 决策默认以回退策略兜底，真实在线 LLM 决策可继续强化。

## 4. 范围定义

### In Scope（当前版本）

- 证据卡建模与 schema 管理。
- Phase 1-6 可执行闭环流程。
- orchestration 状态机、Todo 编排、状态持久化。

### Out of Scope（当前版本）

- 多实例分布式执行与多租户隔离。
- 大规模在线学习/微调系统。
- 完整前端可视化平台。

## 5. 功能需求

- FR-1：工件解析，生成四类 v1 证据卡。
- FR-2：动态证据增强，输出更高质量 v2 卡片。
- FR-3：闭环判定（sufficiency/consistency/attribution）。
- FR-4：基于证据生成 patch plan。
- FR-5：执行 patch 并进入验证。

## 6. 非功能需求

- 可追溯：关键结论必须可回溯到证据来源。
- 一致性：文档、schema、模型定义一致。
- 可恢复：流程中断可恢复，状态可重放。
- 可维护：路径稳定、命名规范、模块边界清晰。

## 7. 风险与对策

- 风险：证据不足导致误修。
  - 对策：closure 失败必须生成 gap/todo，阻断盲目进入 patch。

- 风险：文档与实现漂移。
  - 对策：以代码为准，文档明确“已实现/目标态”。

- 风险：结构依赖遗漏导致联动破坏。
  - 对策：structural card 强制记录 dependency edges 与 co-edit groups。
