## Plan: Phase2 LLM增强与Navigator协同

在保持现有四类 Phase2 extractor 主体不变的前提下，新增一个统一的 LLM 增强层，对 Symptom/Localization/Constraint/Structural 全量执行二次语义推理，并通过 CodebaseNavigator 做建议验证与证据回填。输出策略采用覆盖式升级：Phase2 规则产物为 v2，增强后落盘为 v3，供后续 closure-checker 与 patch-planner 直接消费。这样能最大化语义理解能力，同时通过“LLM建议必须经本地检索/AST验证”来控制幻觉风险。

**Steps**
1. Phase A - 设计与契约冻结
1.1 明确 LLM 增强层职责边界：只增强证据，不改变 card 数据模型与阶段所有权；不得直接做 patch 结论。  
1.2 定义统一增强输入上下文协议：每个 extractor 传入 card v2、关键仓库片段、检索摘要、sufficiency 现状。  
1.3 定义统一增强输出协议：新增证据、补充映射、冲突标记、增强后 sufficiency_notes；禁止新增 schema 外字段。  
1.4 定义版本策略：规则层写 v2，增强层写 v3，保留 card_versions/v2 与 card_versions/v3。  

2. Phase B - CodebaseNavigator 协同能力扩展（可与 Step 3 并行）
2.1 为 Navigator 增加“建议验证”接口：验证 file_path/symbol/line 是否真实存在，可由 AST+文本双通道校验。  
2.2 为 Navigator 增加“上下文提取”接口：按候选位置抽取最小代码窗口，供 LLM 语义推理使用。  
2.3 为 Navigator 增加“置信度汇聚”接口：融合规则信号与 LLM 信号，输出最终贡献值并写回 evidence_source。  
2.4 增加缓存策略（AST、grep、调用图）避免全量增强导致的重复扫描。  

3. Phase C - 新增统一 LLM 增强编排层（依赖 Step 1；可与 Step 2 部分并行）
3.1 在 Phase2 中新增增强调度器，顺序执行 Symptom→Localization→Constraint→Structural 的增强步骤。  
3.2 每一步增强采用同一流程：构造结构化提示 → 调用 LLM → schema 校验 → Navigator 验证 → 合并回 card。  
3.3 为每类 card 定义最小增强目标：  
- Symptom：补全触发条件语义、异常上下文、观测-预期间桥接证据。  
- Localization：补全高阶候选（wrapper/工厂/配置驱动入口）与 interface-to-code 映射。  
- Constraint：补全隐式约束、兼容性边界、边界条件义务。  
- Structural：补全间接依赖、协同编辑组与传播风险说明。  
3.4 统一冲突处理：LLM 增强与规则结论冲突时不覆盖原结论，写入冲突标记与 sufficiency_notes。  

4. Phase D - Phase2 主流程接入与落盘策略（依赖 Step 3）
4.1 调整 Phase2 入口流程：先产出四张 v2，再执行增强并产出四张 v3。  
4.2 调整 latest 文件与版本快照写入逻辑，确保 evidence 下主文件指向 v3，且保留 v2 快照。  
4.3 调整 phase2_summary 输出，新增增强调用元数据（耗时、调用次数、验证通过率、冲突数）。  

5. Phase E - 质量门禁与可观测性（依赖 Step 4）
5.1 为增强层增加结果校验：字段合法性、证据来源完整性、置信度范围校验。  
5.2 增加失败回退：单卡增强失败时保留 v2 并标注 partial，不中断其余卡增强。  
5.3 增加审计日志：记录每条 LLM 建议是否通过 Navigator 验证及拒绝原因。  

6. Phase F - 文档与契约同步（可与 Step 5 并行，最终合并）
6.1 更新架构文档，说明 Phase2 由“规则提取层 + LLM 增强层”组成。  
6.2 更新接口文档，补充 v2→v3 版本语义与增强来源约束。  
6.3 更新 worker 规范（至少 symptom/localization/constraint/structural），明确新完成标准与失败出口。  
6.4 如模型变更影响 schema 真源，执行 schema 生成脚本并核对产物一致性。  

**Relevant files**
- d:/demo/src/evidence_extractors_phase2.py — 现有四类 extractor、CodebaseNavigator、run_phase2_extraction_dynamic；主要接入点。  
- d:/demo/src/evidence_cards.py — card 模型真源；用于约束增强输出不越界。  
- d:/demo/src/artifact_parsers_llm.py — 可复用 Phase1 的 LLM 调用与结构化输出模式。  
- d:/demo/src/orchestrator.py — 预留多阶段编排定义，可同步补充增强层 agent 职责描述。  
- d:/demo/main.py — 当前主流程入口；需要让 Phase2 默认消费 v3。  
- d:/demo/docs/10-architecture.md — 架构分层与阶段依赖规则。  
- d:/demo/docs/20-interfaces.md — card 接口和版本约束补充。  
- d:/demo/docs/workers/localization-extractor/10-spec.md — 高优先级增强 worker 规范。  
- d:/demo/docs/workers/constraint-extractor/10-spec.md — 高优先级增强 worker 规范。  
- d:/demo/docs/workers/symptom-extractor/10-spec.md — 症状增强契约同步。  
- d:/demo/docs/workers/structural-extractor/10-spec.md — 结构增强契约同步。  
- d:/demo/scripts/generate_evidence_schemas.py — 若模型调整需要重生成 schema。  

**Verification**
1. 在示例实例目录运行 Phase1+Phase2，确认 evidence 主文件版本为 v3，且 card_versions 同时存在 v2 与 v3。  
2. 对四张 card 执行 schema 校验，确认未出现 schema 外字段、枚举非法值、置信度越界。  
3. 抽样检查每张 card 的新增结论，确认存在 evidence_source 且 source_path 能在 repo/artifacts 中定位。  
4. 注入一组故意错误的 LLM 建议，验证 Navigator 拒绝机制生效并写入审计日志。  
5. 对比增强前后 sufficiency_status 与 sufficiency_notes，确认增强是补证而非静默覆盖。  
6. 运行至少 2 个现有 workdir 案例，确认 Phase2 总体成功率与耗时在可接受范围。  

**Decisions**
- 已确认：增强策略采用全量增强（四类 extractor 都做 LLM 二次推理）。  
- 已确认：落盘策略采用覆盖式升级，规则层产出 v2，增强层产出 v3 并作为 latest。  
- 包含范围：Phase2 增强层、Navigator 协同、版本与文档同步、验证闭环。  
- 排除范围：不在本次计划中实现 Phase3 closure-checker 逻辑重构；仅确保其可消费更高质量 v3 输入。  

**Further Considerations**
1. 模型调用策略建议：先固定单模型（与 Phase1 同源）保证一致性，再按卡类型做分模型优化。  
2. 性能策略建议：默认全量增强但支持实例级开关，在批量回放时可降级为仅增强 Localization 与 Constraint。  
3. 置信度策略建议：保留“规则信号优先”，LLM 贡献作为增量项，避免单次推理主导最终排序。