# 基于证据闭环的软件工程修复 Agent

基于 Claude Agent SDK 的软件工程缺陷修复 Agent，采用 Evidence Closure 理论框架。

## 理论核心

修复失败的主要原因不是 agent 不会生成 patch，而是缺乏判断当前信息是否足以支持 patch commitment 的能力。本系统通过四类必备 evidence 的闭环检查来解决这个问题：

1. 症状证据（Symptom Evidence）：现在坏在哪里，修好后应表现为什么样。
2. 定位证据（Localization Evidence）：应该改哪里。
3. 约束证据（Constraint Evidence）：什么修改才算正确，什么不能改坏。
4. 结构证据（Structural Evidence）：哪些位置必须一起改，修改之间是什么依赖关系。

## 当前架构

当前为单栈 orchestration 架构：

1. 入口：main.py。
2. CLI：src/app/cli.py。
3. Pipeline：src/pipelines/run_repair_workflow.py。
4. 编排核心：src/orchestration/。
5. Worker 注册：src/workers/registry.py。

执行策略为硬失败：执行器缺失、导入失败、运行异常都会直接失败，不做降级成功。

## 目录结构

workdir/{instance_id}/

1. repo/：目标仓库
2. artifacts/：Phase 1 输入
3. evidence/：Phase 1/2 证据卡与版本快照
4. closure/：Phase 3 产物
5. plan/：Phase 4 产物
6. patch/：Phase 5/6 产物
7. logs/：编排事件日志

## 可用命令

仅支持 dynamic 入口：

1. python main.py face_recognition_issue_001 --dynamic
2. python main.py research_agent_issue_002 --dynamic

说明：

1. --phase1-only 与 --phase2-only 已移除。
2. --resume、--fail-fast、--from-phase 当前在 CLI 层会被忽略。

## 阶段状态

1. Phase 1：已实现（最小可运行版）
2. Phase 2：已实现（最小可运行版）
3. Phase 3：已实现（最小可运行版）
4. Phase 4：已实现（最小可运行版）
5. Phase 5：已实现（最小可运行版）
6. Phase 6：已实现（最小可运行版）

后续将继续增强 Phase1/Phase2 的抽取质量与闭环策略精度。
