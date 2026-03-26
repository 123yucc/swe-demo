# 架构说明

## 系统目标

构建一个 evidence-closure-aware 修复流程：先建立可追溯证据，再做 patch 规划与执行，降低“盲改”风险。

## 当前实现总览（v0.5.0）

- 入口：`main.py`
- 主能力：
  - Phase 1：`run_phase1_parsing`（LLM 驱动解析）
  - Phase 2：`run_phase2_extraction_dynamic`（动态证据提取）
  - Dynamic Scheduler：`src/scheduler/`
  - Memory：`src/memory/`
  - LLM Enhancement：`src/llm_enhancement/`

## 阶段与 Worker

调度注册表定义于 `src/scheduler/models.py:create_default_registry`：

1. Phase 1：`artifact-parser`
2. Phase 2：`symptom-extractor` / `localization-extractor` / `constraint-extractor` / `structural-extractor` / `llm-enhancer`
3. Phase 3：`closure-checker`
4. Phase 4：`patch-planner`
5. Phase 5：`patch-executor`
6. Phase 6：`validator`

## 关键边界与目录

实例目录：`workdir/<instance_id>/`

- `artifacts/`：输入工件
- `evidence/`：证据卡
- `evidence/card_versions/`：卡片版本快照
- `plan/`：调度 Todo 与 patch plan
- `logs/`：调度事件日志
- `.workflow/`：工作流状态持久化
- `.memory/`：记忆数据（长期/短期）
- `patch/`：补丁产物（由 patch 执行阶段使用）

## 状态机与持久化

- 工作流状态模型：`WorkflowState`（`src/scheduler/models.py`）
- 调度器：`Scheduler`（`src/scheduler/scheduler.py`）
- 核心能力：
  - ready-set 计算（worker + todo）
  - 重试与失败记录
  - closure block 分支处理（回退 Phase 2 + 生成 gap todos）

## 实现约束（按当前代码）

- `--dynamic` 使用 Scheduler 流程；`--phase1-only/--phase2-only` 走直连执行。
- 调度器执行器目前引用 `..evidence_extractors`、`..closure_checker` 等模块；仓内主要已实现模块是 `evidence_extractors_phase2.py`。
- 因此“全 6 阶段端到端”是目标架构，当前稳定可用主路径仍以 Phase 1/2 + 动态调度框架为主。
