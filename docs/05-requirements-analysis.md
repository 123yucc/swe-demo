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

- Phase 1：`run_phase1_parsing`（LLM 驱动）
- Phase 2：`run_phase2_extraction_dynamic`（动态证据提取）
- Dynamic Scheduler 框架（状态、todo、重试、分支）
- Memory（long-term/short-term）与 LLM 增强层模块

当前限制：

- 调度器 6 阶段执行器已建模，但部分后续模块在仓内未形成完整稳定闭环。
- 因此“完整 1~6 端到端自动修复”属于目标态，不应被描述为已全面可用能力。

## 4. 范围定义

### In Scope（当前版本）

- 证据卡建模与 schema 管理。
- Phase 1/2 可执行流程。
- 动态调度、Todo 编排、状态持久化。

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
