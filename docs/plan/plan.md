## Plan: 项目级 Long/Short Memory 管理设计

目标是在不改动现有证据卡真源机制的前提下，设计一套可落地的 longterm/shortterm memory 管理方案：longterm 用于跨 issue 学习与复用，shortterm 用于单 issue 会话导航与恢复；并以“最终 patch commit 后清理短期记忆”为生命周期边界。

**Steps**
1. Phase A - 约束固化（输入对齐）
   - 基于文档与代码现状确认不可破坏边界：evidence 卡片单一真源、版本快照链、sufficiency 门禁、阶段单向依赖。
   - 明确本次范围：仅设计方案，不涉及代码改造与脚本实现。
2. Phase B - 记忆分层模型设计（核心）
   - 定义 longterm 与 shortterm 的职责边界：
     - longterm：跨 issue 复用知识（修复模式库、工具/检索策略、置信度权重学习、失败案例反模式）。
     - shortterm：当前 issue 运行态（阶段进度、证据缺口、决策审计、工作缓存）。
   - 定义读写原则：longterm 低频高价值写入；shortterm 高频可丢弃写入。
3. Phase C - 存储布局与命名规范（可执行结构）
   - 设计 longterm 目录：workdir/.memory/longterm/，按主题分文件（patterns、retrieval、weights、antipatterns）。
   - 设计 shortterm 目录：workdir/{instance_id}/.memory/shortterm/，按会话状态分文件（phase_state、evidence_gap、decision_audit、runtime_cache）。
   - 统一命名与版本语义：snake_case 文件名、schema_version 字段、created_at/updated_at UTC。
4. Phase D - 数据模型与索引策略（字段级）
   - 为 longterm 设计最小实体：
     - memory_item（id、topic、signal、action、outcome、confidence、evidence_refs、last_used_at、decay_score）
     - weight_profile（source_type、match_type、weight、sample_size、win_rate）
     - anti_pattern（trigger、bad_action、impact、avoidance）
   - 为 shortterm 设计最小实体：
     - session_state（instance_id、phase、status、checkpoint）
     - evidence_gap（card_type、gap_type、required_signal、priority）
     - decision_log（decision、rationale、inputs、result）
   - 增加轻量索引字段：tags、issue_type、module_scope，支持快速检索。
5. Phase E - 生命周期与治理策略
   - shortterm 生命周期：issue 完成并最终 patch commit 后触发清理，仅保留必要审计摘要（可选）。
   - longterm 生命周期：保留并定期衰减（基于 last_used_at 与 outcome 成功率）。
   - 冲突与一致性：通过 revision/version 做幂等更新，避免并发覆盖。
6. Phase F - 与现有流程的接入蓝图（仅设计，不实现）
   - 读入点（建议）：Phase 1 前加载 longterm 检索策略与修复模式候选。
   - 写入点（建议）：Phase 2 结束后写入结构化“有效证据组合”；Phase 3 决策后写入“闭环结论”；Phase 5/6 结果回写 outcome。
   - 不改动点：evidence/*.json 与 card_versions/ 作为事实真源维持不变，memory 只做旁路增强。
7. Phase G - 验收标准与风险清单
   - 验收：可解释（每条记忆可追溯）、可检索（按 issue/module/topic 查找）、可清理（shortterm 自动清除）、可演进（schema_version）。
   - 风险：知识污染、过拟合权重、日志膨胀、错误复用；对应约束：准入阈值、置信度下限、容量上限、定期回收。

**Relevant files**
- d:/demo/docs/10-architecture.md — 6 阶段职责与依赖边界（记忆接入不越界）。
- d:/demo/docs/20-interfaces.md — evidence 输出接口与卡片约束（memory 不替代真源）。
- d:/demo/docs/30-naming-rules.md — 命名规则（memory 文件命名对齐）。
- d:/demo/src/evidence_cards.py — sufficiency 与 evidence source 数据结构参考。
- d:/demo/src/evidence_extractors_phase2.py — 置信度权重学习的当前计算逻辑参考。
- d:/demo/src/orchestrator.py — 运行阶段事件与未来读写钩子参考。
- d:/demo/workdir/research_agent_issue_002/evidence/localization_card.json — 当前高密度证据样例，验证索引与冷热分层必要性。

**Verification**
1. 结构验证：检查 longterm/shortterm 数据模型是否都能映射到现有 phase 输入输出，不引入反向依赖。
2. 可追溯验证：任一 longterm 条目可回溯到 issue 与 evidence_refs（不允许无来源记忆）。
3. 生命周期验证：定义并演练“最终 patch commit 后短期清理”的触发条件与保留白名单。
4. 命名/字段验证：对照命名规则与 schema_version，确保新增 memory 文件可被稳定解析。
5. 风险验证：模拟错误模式写入，确认准入阈值与衰减策略可抑制污染。

**Decisions**
- 已确认仅输出设计方案，不做代码实现。
- longterm memory 覆盖：修复模式库、工具与检索策略、置信度权重学习、失败案例与反模式。
- shortterm memory 生命周期：每个 issue 在最终 patch commit 后清理。
- 本次明确不包含：数据库选型、向量检索引擎落地、迁移脚本开发、CI 集成。

**Further Considerations**
1. shortterm 清理后是否保留极简审计摘要（建议保留，便于复盘；可选全清理）。
2. longterm 准入阈值建议：至少 2 次正向 outcome 才提升为可复用模式。
3. 权重学习建议先半自动：先记录建议权重，不自动覆盖默认权重，降低回归风险。
