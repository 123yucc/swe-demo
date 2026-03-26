## Plan: 动态调度与Todo编排改造

将现有 main.py 中写死的 Phase1→Phase2 串行调用，升级为“规则驱动的工作流引擎”：用 DAG 依赖 + 门禁条件 + 状态机来动态决定下一批可执行 workers，并把待办事项(todo)作为一等对象持久化与推进。这样既保持 docs 中的强约束兼容（尤其 closure gate），又支持并行执行、重试、断点续跑和后续扩展到 Phase 3-6。

**Steps**
1. Phase A: 建立调度领域模型（阻塞后续）
2. 定义 WorkflowState（phase、worker 状态、重试次数、失败原因、最新 cards 版本、当前 todo 队列快照），持久化到 workdir/<instance_id>/.workflow_state.json；支持 resume。
3. 定义 WorkerSpec/TaskSpec 数据结构：worker_id、输入契约、输出契约、depends_on、gate_conditions、can_parallel、max_retries、timeout、produces_todo。
4. 定义 TodoItem 数据结构：todo_id、来源（closure/worker/runtime）、优先级、依赖、执行建议、完成判据、关联证据路径。
5. Phase B: 建立规则驱动注册中心（依赖 A）
6. 在独立 registry 模块中注册 Phase 1-6 workers 与依赖图（DAG），把 currently hardcoded 顺序转成声明式配置；保持 worker 标识符与 docs 命名一致。
7. 把闭环门禁做成可执行规则：
8. 规则 R1: closure_report.overall_status=allow 才允许 patch-planner。
9. 规则 R2: closure_report.overall_status=block 时，不进入 patch-planner，改为生成“补证 todo”并回流 Phase 2。
10. 规则 R3: patch_plan.json 存在且 schema/字段校验通过，才允许 patch-executor。
11. 规则 R4: card version 必须单调递增，不允许覆盖历史快照。
12. Phase C: 实现调度循环与并行执行（依赖 A+B）
13. 实现 Scheduler.tick()：
14. 计算 ready set（依赖满足+门禁满足）。
15. 将 ready workers 转为可执行任务并发执行（仅对 can_parallel=true 且无互斥资源冲突的 worker 并发）。
16. 收集结果并更新 WorkflowState 与 todo 状态。
17. 若无 ready 且仍有 blocked，输出阻塞诊断并安全终止（避免死循环）。
18. 实现失败策略：指数退避重试、超过阈值后将失败原因转成诊断 todo，并标记 workflow 为 needs_input/failed。
19. Phase D: 动态 Todo 编排（依赖 C，可与 E 并行）
20. 统一 todo 来源：
21. closure-checker gaps -> 高优先级补证 todo；
22. worker 运行时发现缺失 artifact/路径异常 -> 中优先级修复 todo；
23. 调度器阻塞诊断 -> 技术债/配置修复 todo。
24. todo 状态机：pending -> ready -> running -> done/blocked/abandoned，支持依赖解除自动晋升 ready。
25. todo 持久化：workdir/<instance_id>/plan/todo_queue.json（审计可追踪）；同步输出简版摘要到 stdout/log。
26. Phase E: 接入现有 orchestrator 与 CLI（依赖 C，和 D 并行后汇合）
27. 保留现有 run_phase1_only/run_phase2_only 作为兼容入口；新增 run_dynamic_workflow（默认 full 路径）。
28. main.py 将当前 run_full_workflow 改为调用 dynamic scheduler；命令行新增 --resume、--max-parallel、--fail-fast、--from-phase。
29. orchestrator.py 中 get_agent_definitions 保留，但把 phase 写死 prompt 迁移为参数化模板（phase_id/worker_id 注入）。
30. Phase F: 兼容性与观测强化（依赖 D+E）
31. 增加结构化运行日志（每个 task 的开始/结束/耗时/输入输出摘要/门禁判定）；落盘到 workdir/<instance_id>/logs/scheduler_events.jsonl。
32. 加入路径兼容补丁策略：同时识别 expected_and current_behavior.md 与 expected_and_current_behavior.md，遇到冲突产生日志与 todo 提示（避免 Windows 环境踩坑）。
33. 在 evidence 写入层加入原子写与版本冲突检测，避免并发 worker 导致 card 覆盖。
34. Phase G: 测试与回归验证（依赖 F）
35. 单元测试：DAG 拓扑、ready set 计算、门禁规则、todo 状态机、重试/失败迁移。
36. 集成测试：
37. 场景 S1（闭环 allow）：Phase1->2->3->4->5 全流程。
38. 场景 S2（闭环 block）：Phase3 产出 gaps，自动回流 Phase2，todo 驱动补证后再进 Phase3。
39. 场景 S3（缺少 patch_plan）：阻止进入 Phase5 并生成阻塞 todo。
40. 场景 S4（中断恢复）：执行中断后 --resume 从 state 继续。

**Relevant files**
- d:/demo/main.py — 当前写死流程入口；改为 dynamic scheduler 主入口并保留兼容参数。
- d:/demo/src/orchestrator.py — 现有 AgentDefinition 与 options/hook 入口；改为接入调度器并参数化 worker prompt。
- d:/demo/src/__init__.py — 对外导出新增 dynamic workflow 运行入口。
- d:/demo/src/artifact_parsers_llm.py — 复用 Phase1 执行函数作为 WorkerSpec executor。
- d:/demo/src/evidence_extractors_phase2.py — 复用 Phase2 执行函数并按 extractor 依赖拆分任务粒度。
- d:/demo/src/evidence_cards.py — card version 与写入契约检查复用点。
- d:/demo/docs/10-architecture.md — 6 阶段依赖与门禁约束参考真源。
- d:/demo/docs/20-interfaces.md — 输入输出路径与 card/evidence 字段契约。
- d:/demo/docs/30-naming-rules.md — worker/card 命名与 version 规则。
- d:/demo/docs/workers/closure-checker/10-spec.md — allow/block 门禁行为约束。
- d:/demo/docs/workers/patch-planner/10-spec.md — 计划产物契约。
- d:/demo/docs/workers/patch-executor/10-spec.md — 执行阶段输入依赖与输出契约。

**Verification**
1. 运行单元测试，验证调度器核心算法：依赖解析、ready set、gate 判定、todo 状态迁移、重试策略。
2. 构造最小实例目录，执行动态 full workflow，确认 worker 执行顺序符合 DAG 且可并行部分确实并行。
3. 人工注入 closure_report=block，验证系统不会进入 patch-planner，而是生成补证 todo 并回流 Phase2。
4. 人工删除/破坏 patch_plan.json，验证 patch-executor 被门禁拦截并记录阻塞原因。
5. 连续执行两轮 evidence 更新，验证 version 单调递增且 card_versions 快照完整。
6. 强制中断进程后使用 --resume 恢复，验证任务不重复执行且状态一致。
7. 回归现有 --phase1-only/--phase2-only，确保兼容路径仍可用。

**Decisions**
- 采用规则驱动调度（已确认）：DAG + 门禁 + 状态机；不采用纯 LLM 调度。
- 范围包含：动态调度、动态 todo 编排、断点续跑、观测与兼容性加固。
- 范围不包含：重写各 worker 的业务抽取算法；仅封装其执行与编排方式。
- 默认保持文档契约不变：路径、命名、schema 字段、sufficiency_status 枚举。

**Further Considerations**
1. todo 优先级策略建议固定为 P0=closure gaps, P1=gate blockers, P2=quality improvements，先保证可预测性再引入自适应排序。
2. 并行度建议初始限制为 2-3，先避免 evidence 并发写冲突，再根据日志调优。
3. 若后续要引入 LLM 辅助排序，建议仅在同优先级 ready set 内排序，不参与门禁判定。