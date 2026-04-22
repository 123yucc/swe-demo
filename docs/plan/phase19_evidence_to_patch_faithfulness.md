## Plan: Phase 19 — 证据到补丁的忠实闭环（聚合一致性 + Findings 结构化 + Patch Readiness + Patch Faithfulness）

**背景与根因**

phase18 已经把 evidence 质量控制从“closure-checker 单点兜底”推进到了“机械不变量 + AuditManifest + anti-hallucination + preserved_findings + deep-search reflection”的多层体系，系统在“证据是否真实”这一维上明显更强了。但对照当前所有 agent 实现，结合 issue002 的 patch 质量问题，可以看到剩余瓶颈已经从 **evidence factuality** 转移到了 **evidence 能否稳定驱动高质量 patch**。

当前代码里至少还有四层未闭合问题：

1. **EvidenceCards 全局聚合仍可能残留过时 observation。**
   虽然 `update_localization` 已改为 scope-based replace，`reset_requirement_for_rework(...)` 也会删除对应 scope，但当前聚合模型仍是把 `_scoped_store` 扁平化后回填到 `localization.* / structural.* / constraint.*`。这保证了“单 scope 替换”，却没有显式定义“哪些全局字段必须随 requirement 重开而同步收缩/重算、哪些是可以跨轮沉淀的稳定事实”。phase18 文档也明确把“跨 scope 残留”留作后续问题。结果是 patch-planner 仍可能在 `SharedWorkingMemory.format_for_prompt()` 中读到**当前 requirement 已被纠正、但全局 evidence 仍混有旧观察**的状态。

2. **Requirement findings 仍然过于自由文本，下游消费不稳定。**
   `RequirementItem.findings` 与 `DeepSearchReport.requirement_findings` 目前仍是单个字符串；phase18 通过 `findings_anti_hallucination` 与 `prescriptive_boundary_self_check` 提高了真实性，但 patch-planner 仍要靠 `_extract_prescriptive_snippets(...)` 正则从自由文本里猜哪些内容是代码事实、哪些是修法约束、哪些是边界案例。这种“先自由生成，再靠正则回捞结构”的方式脆弱，尤其在 issue002 这类要求严格遵守 API 迁移边界的题上，下游很容易只抓住一部分约束，遗漏真正决定 patch 形状的 requirement 语义。

3. **closure 通过不等于 patch 已准备好。**
   当前 closure-checker 审的是“verdict 是否被代码支持”“findings 是否不幻觉”“prescriptive 是否通过边界自查”；但系统还没有单独判断：这些 evidence 是否已经足够具体、完整、无冲突到可以指导 patch。比如某条 requirement 可能已被正确判成 `TO_BE_MISSING`，但 evidence 中没有明确 edit locus、没有稳定 patch constraints、没有覆盖 structural co-edit；这种情况下 evidence 在“事实正确”意义上可能已 closure approved，但在“可生成高质量 patch”意义上仍未 ready。

4. **patch 阶段仍缺少‘忠实度’校验。**
   从 issue002 的失败模式看，坏 patch 不一定来自 evidence 错，也可能来自 patch-planner / patch-generator 的边界漂移：擅改 API 签名、留下 alias 规避真实迁移、为了“满足 requirements”而重写测试结构。当前 phase18 只把 `preserved_findings` 作为 hard constraint 传了下去，但没有机制独立判断 patch 是否真正遵守 requirement 原文、new interface 规范、structural co-edit，以及“最小修改”这类 benchmark 关键约束。也就是说，系统目前能较好判断“证据真不真”，却还不能稳定判断“补丁忠不忠实”。

设计哲学（承接 phase16/17/18）：

- **Evidence correctness 不是终点，Patch faithfulness 才是闭环终点。** phase19 不再重复加强“事实核验”，而是补上“证据如何稳定、完整、可执行地流向 patch”。
- **凡是可代码化的 readiness / faithfulness 条件，一律前移到代码。** closure-checker 审“真不真”，readiness gate 审“够不够”，patch self-review 审“守不守约束”。
- **自由文本要继续收缩。** findings 既然要供 patch 阶段消费，就不应只是一段 prose；应至少拆出“已验证代码事实 / patch constraints / edge cases”等受限结构。
- **对 benchmark 任务的边界要机械保护。** API 签名、新接口声明、调用点迁移、测试最小修改范围，都不应只靠 patch agent 自觉遵守。

---

**Steps**

### 19.A — Evidence 聚合一致性：显式治理跨 scope 残留与全局重建

phase16 用 `_scoped_store` 解决了“同一 requirement 多轮 deep-search 结果 append 污染”的问题，但现在 `_rebuild_aggregate_view()` 的语义仍偏“把所有 scope 扁平拼起来”，而不是“按字段生命周期重建全局真相”。这在 rework 频繁、多个 requirement 指向相同代码区域时，会让全局 evidence 带着过时 observation 继续流向 patch-planner。

- 在 [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) 之上显式定义**聚合策略分层**：
  - `localization.* / structural.* / similar_implementation_patterns`：明确为 **scope-derived aggregate**，任何 requirement reset 后，聚合视图必须完全由剩余 scopes 重建，不允许保留孤儿条目。
  - `constraint.behavioral_constraints / semantic_boundaries / backward_compatibility`：从当前 deep-search 视角看属于“代码现状观察”，但要补充规则区分“可跨 requirement 共享的稳定事实”与“只在某条 requirement 语境下成立的局部观察”。
- 在 [src/models/context.py](src/models/context.py) 或 [src/models/evidence.py](src/models/evidence.py) 为 EvidenceCards 增加一层可选的内部聚合元数据结构（例如 `aggregation_meta` 或按字段注释说明），用于表达哪些字段是 scope-derived，哪些字段是 parser-owned immutable。
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 `_persist_report_findings(...)` 后引入显式的 `normalize_evidence_cards()` / `recompute_aggregate_views()` 步骤，让“persist 单条 req 结果”和“刷新全局 evidence 视图”在代码里成为两个可追踪动作。
- 扩展 [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) 的 `reset_requirement_for_rework(...)`：
  - 除了清理 requirement verdict/findings/scope 外，还应标记哪些 aggregate 字段需要强制重算
  - 如果后续定义了“稳定事实缓存”，这里也要同步撤销与该 requirement 强绑定的缓存项
- 依赖：无。
- 验证：
  - 构造 2 轮对同一 req 的 deep-search，第二轮删除一条第一轮曾写入的 `must_co_edit_relations`，聚合视图里该条必须消失
  - 构造两个 req 指向同一文件，reset 其中一个后，全局 `similar_implementation_patterns` 只保留另一个 scope 的条目
  - 回归 issue002 / issue001，检查 evidence.json 在 rework 后没有残留已被推翻的 observation

### 19.B — Findings 结构化：从自由文本变成可下游消费的约束对象

当前 `DeepSearchReport` 里既保留了 requirement verdict，又混有 `confirmed_defect_locations / new_suspects / open_questions` 等自由探索字段；而真正驱动 patch 的 `requirement_findings` 仍是裸字符串。这会让 patch-planner 的输入过于模糊，也让 closure-checker 只能审“这段 prose 有没有幻觉”，而不是审“这段输出的结构是否足够支撑 patch”。

- 在 [src/models/report.py](src/models/report.py) 为 `DeepSearchReport` 新增一个受限的 findings 结构，例如：
  ```python
  class RequirementFindings(BaseModel):
      verified_code_facts: list[str]
      violation_reasoning: list[str]
      patch_constraints: list[str]
      edge_cases: list[str]
  ```
  并让 `requirement_findings` 从 `str` 迁移为该结构（或新增 `requirement_findings_structured` 并在过渡期保留旧字符串字段以兼容）。
- 在 [src/models/evidence.py](src/models/evidence.py) 的 `RequirementItem` 上同步增加结构化 findings 字段，使 EvidenceCards 成为下游唯一可消费的结构化真相，而不是 “string findings + regex recovery”。
- 重写 [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) 的 DEEP_SEARCH_SYSTEM_PROMPT / REFLECTION_SYSTEM_PROMPT：
  - 第一轮输出时就要求把 findings 拆进固定槽位，而不是先写 prose
  - reflection 轮不仅检查 token traceability，还要检查：
    - `verified_code_facts` 的每一项是否都能在 Read 结果里找到支撑
    - `patch_constraints` 是否只是对 `verified_code_facts` 和 requirement 文本的受约束推导，而不是新幻觉
    - `edge_cases` 是否真正对应 requirement 的 boundary
- 更新 [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 与 [src/orchestrator/audit.py](src/orchestrator/audit.py)：
  - `findings_anti_hallucination` 从“检查 backtick snippet”升级为“检查 structured facts / constraints 是否都可追溯”
  - `prescriptive_boundary_self_check` 直接消费 `patch_constraints` + `edge_cases`，不再只靠自由文本关键词触发
- 在 [src/agents/patch_planner_agent.py](src/agents/patch_planner_agent.py) 中，`preserved_findings` 的来源改为结构化字段中的 `patch_constraints` / `edge_cases`，尽量移除 `_extract_prescriptive_snippets(...)` 这种正则补丁逻辑，或将其降级为兼容兜底。
- 依赖：19.A 不强依赖，但两者互补。
- 验证：
  - 单测 pydantic schema：旧 evidence.json 能否兼容加载；新 deep-search 输出必须含结构化 findings
  - 构造含 backtick + boundary 的样例，closure-checker 应直接在 structured fields 上做 PASS/FAIL
  - patch-planner 输出的 `preserved_findings` 应和 `patch_constraints / edge_cases` 一一对应，而不是自由抽取

### 19.C — Patch Readiness Review：在 closure 之后新增“够不够指导 patch”的独立 gate

phase18 的 closure 审计已经很强，但它回答的是“evidence 对不对”，不是“这些 evidence 是否足以驱动 patch-planner 做出稳定决策”。phase19 需要在 `Closed -> PatchPlanning` 之间增加一个新层：**patch readiness review**。

- 在 [src/orchestrator/guards.py](src/orchestrator/guards.py) 新增 `check_patch_readiness(evidence) -> list[str]` 或返回结构化缺陷列表，优先代码化可机械判断的 readiness 条件：
  - 每个非 compliant requirement 是否有明确 `evidence_locations`
  - 是否至少存在一个可落到文件/函数级别的 edit locus
  - 是否有非空 `patch_constraints`
  - 是否存在 overlapping requirements 给出互相冲突的 patch constraints
  - `structural.must_co_edit_relations` 是否对涉及公共接口的 requirement 给出足够 co-edit 信息
- 在 [src/models/audit.py](src/models/verdict.py) 或新模型文件中定义 `PatchReadinessVerdict` / `ReadinessIssue`，与 `ClosureVerdict` 分离，避免再次把不同语义混在 closure-checker 里。
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的状态机中，在 `Closed` 与 `PatchPlanning` 之间插入一层 readiness gate：
  - 机械检查不通过 → 直接 reopen 对应 req，回 UNDER_SPECIFIED
  - 机械检查通过但仍需要语义判定的情况 → 可新增一个轻量 LLM review（推荐复用 manifest 思路）
- 在 [src/orchestrator/audit.py](src/orchestrator/audit.py) 扩展 manifest-builder：
  - 除 `AuditManifest` 外，再生成一份 `PatchReadinessManifest`
  - 代码决定哪些 requirement 需要 readiness review，LLM 只执行 review
- 如果引入 LLM readiness reviewer，建议优先复用 [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 的 repo-read audit 形态，而不是新造一个完全不同的 agent；但其职责要严格限定为“评估 patch 是否已经有足够证据基础”，不再重复 verdict factuality。
- 依赖：19.B（若引入结构化 findings，readiness gate 才能更稳定）；19.A 提高 aggregate 稳定性也会让 readiness 更可靠。
- 验证：
  - 构造“verdict 正确但没有 patch constraints”的样例，closure 应通过，readiness 应失败
  - 构造 overlapping requirements 各自 findings 正确但 patch constraints 冲突的样例，readiness 应失败并指向对应 req
  - 端到端观察 issue002：如果 evidence 没有明确约束 API 签名和最小测试改动，系统不应直接进入 patch-planning

### 19.D — Patch Faithfulness：给 patch-planner / patch-generator 增加忠实度校验与自省轮

issue002 暴露的不是“patch apply 失败”，而是“patch 偏离 benchmark 任务边界”：擅改 API 签名、用 alias 规避真实迁移、过度重写测试。phase19 需要把这类问题提升成一等概念：**patch faithfulness**。

- 在 [src/models/patch.py](src/models/patch.py) 的 `FileEditPlan` 上新增更强的 requirement 对齐信息，例如：
  - `covered_requirements: list[str]`
  - `forbidden_changes: list[str]`（如“不要改公开签名”“不要保留 alias 兼容层”“不要整体重写测试结构”）
  - `edit_scope_reason: str`（为什么这几个文件就是最小充分改动集）
- 重写 [src/agents/patch_planner_agent.py](src/agents/patch_planner_agent.py) 的 prompt：
  - 不只要求 preserved findings，还要求显式列出“本文件覆盖哪些 RequirementItem / 哪些 new_interfaces / 哪些 backward_compatibility constraints”
  - 明确 benchmark-friendly 反模式：
    - API contract drift
    - alias-based migration escape
    - over-broad test rewriting
  - 把这些反模式转成 plan 层的 `forbidden_changes`
- 在 [src/agents/patch_generator_agent.py](src/agents/patch_generator_agent.py) 引入第二轮 reflection（仿照 deep-search 18.E）：
  - Round 1：按 PatchPlan 执行 SEARCH/REPLACE
  - Round 2：只读审查已生成 diff / 当前文件内容，对照 requirement 原文、new interfaces introduced、preserved_findings、forbidden_changes 做 self-review
  - 如果 self-review 发现：
    - 签名与 `new_interfaces introduced` 不一致
    - 迁移题仍保留 alias 而未改调用点
    - 测试改动超出“迁移/最小更新”范围
    - 结构性 co-edit 漏掉
    则返回 PATCH_INCOMPLETE 并附带 failure reason，触发 orchestrator 进入新的 patch rework 或失败态
- 在 [src/tools/patch_tools.py](src/tools/patch_tools.py) 或 engine 层补充 diff-level 辅助函数：
  - 收集 patch 涉及文件
  - 检测公开函数/类签名是否变化
  - 检测是否新增 alias 型兼容导出
  - 检测测试文件改动规模是否超阈值
  这些检查尽量代码化，不全依赖 LLM 肉眼 diff。
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 中把 PatchSuccess 的判定从“PATCH_APPLIED / no ERROR”升级为：
  - patch 应用成功
  - patch faithfulness checks 通过
  - 若任一不通过，则进入新的 `PATCH_REWORK` / `PATCH_FAILED` 路径
- 依赖：19.B / 19.C 会显著提升该阶段稳定性，但可先行落地最基本的 diff-level faithfulness checks。
- 验证：
  - issue002 作为主回归样例：
    - 若 patch 改了 `hide_qt_warning(pattern: str, logger: str='qt')` 的签名，应被 faithfulness review 拦截
    - 若 patch 在 `log.py` 留 alias 而没真正完成迁移，应被拦截
    - 若 patch 大幅重写 `test_qtlog.py` 结构，应被标成 over-broad test edit
  - 构造一个允许改测试但只需最小迁移的样例，检查系统不会误伤正常的 test relocation

### 19.E — docs、契约更新与回归基线

phase19 一旦落地，CLAUDE.md 与计划文档必须同步更新，不然后续会继续把“closure approved”误解成“可以放心 patch”。

- 更新 [CLAUDE.md](CLAUDE.md)：
  - 在 Architecture / Closure Rules 段新增 readiness 与 patch-faithfulness 两层
  - 更新 Components 表，说明 patch-planner / patch-generator 不再只是“规划/执行补丁”，还承担 requirement-to-edit 对齐与忠实度校验
  - 明确 `EvidenceCards` 的聚合策略与 structured findings 契约
- 更新 [docs/api.md](docs/api.md)：
  - 如果新增了 structured findings schema / patch self-review 轮次，补充 relay 下的预算、structured retry 风险和工具调用影响
- 新 phase19 文档应在文末明确：
  - 与 phase18 的边界：phase18 解决“证据是否真实”，phase19 解决“证据能否稳定产生忠实补丁”
  - 非目标：例如是否要新增独立 post-patch external verifier、是否要做跨实例统计学习
- 依赖：19.A~D。
- 验证：文档中的状态机、组件职责、字段契约与代码实现一致；不再出现“closure-checker 是最终唯一 approver”的旧表述。

---

**Relevant files**

- [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) — scope store、aggregate rebuild、rework reset、evidence normalization 的主入口
- [src/models/evidence.py](src/models/evidence.py) — RequirementItem、结构化 findings、EvidenceCards 字段契约
- [src/models/context.py](src/models/context.py) — EvidenceCards 作为 SoT 的聚合上下文定义
- [src/models/report.py](src/models/report.py) — DeepSearchReport 的 structured findings 输出模型
- [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) — findings 结构化输出 + reflection 自查
- [src/orchestrator/guards.py](src/orchestrator/guards.py) — patch-readiness 机械 gate
- [src/orchestrator/audit.py](src/orchestrator/audit.py) — readiness manifest / audit scope builder
- [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) — 可能复用为 readiness reviewer，或最少要消费 structured findings
- [src/models/audit.py](src/models/audit.py) — readiness / faithfulness 结果模型
- [src/models/verdict.py](src/models/verdict.py) — 闭环 verdict 体系与新 readiness verdict 的边界
- [src/models/patch.py](src/models/patch.py) — requirement-to-file mapping、forbidden changes、faithfulness 元数据
- [src/agents/patch_planner_agent.py](src/agents/patch_planner_agent.py) — 把 requirement 约束转成显式 plan 契约
- [src/agents/patch_generator_agent.py](src/agents/patch_generator_agent.py) — patch reflection/self-review
- [src/tools/patch_tools.py](src/tools/patch_tools.py) — diff-level mechanical checks 的潜在落点
- [src/orchestrator/engine.py](src/orchestrator/engine.py) — 在 Closed 与 PatchPlanning 之间插入 readiness / faithfulness gate
- [docs/plan/done/phase16_RequirementItem.md](docs/plan/done/phase16_RequirementItem.md)
- [docs/plan/done/phase17_closure_audit.md](docs/plan/done/phase17_closure_audit.md)
- [docs/plan/done/phase18_evidence_quality.md](docs/plan/done/phase18_evidence_quality.md)
- [docs/api.md](docs/api.md)
- [CLAUDE.md](CLAUDE.md)

**Decisions**

- **phase19 不重复加强 parser**：当前 parser 的职责边界已较清晰，phase19 的重点不是再改 requirement 抽取，而是让 requirement findings 与 aggregate evidence 更可消费。
- **closure 与 readiness 分层**：
  - closure = factuality / consistency / anti-hallucination
  - readiness = enough-to-patch / conflict-free-to-plan
  - faithfulness = patch 是否忠实落实 requirements 与边界
- **structured findings 优先于 prompt 扩词**：相比继续给 deep-search / patch-planner 加长 prompt，更可靠的做法是收紧输出 schema。
- **faithfulness 先做 benchmark 关键反模式**：优先机械保护 API 签名漂移、alias 逃课、测试过度改写；更复杂的 patch quality 判断可后续扩展。
- **执行顺序建议**：19.A（聚合一致性）→ 19.B（structured findings）→ 19.C（readiness gate）→ 19.D（patch faithfulness）→ 19.E（docs / 回归）。其中 19.D 可先以最小 diff-level checks 形式提前落地。

---

**Verification（全流程）**

1. **Evidence 聚合一致性**
   - 单测 `_rebuild_aggregate_view()` / `reset_requirement_for_rework()`：同一 scope 替换与 reset 后，旧 observation 不残留
   - 多 requirement 重叠文件场景下，aggregate view 与 surviving scopes 精确一致

2. **Findings 结构化**
   - DeepSearchReport 新 schema 单测：必须输出 `verified_code_facts / patch_constraints / edge_cases` 等字段
   - 旧 evidence.json 兼容加载测试：若采用双字段过渡，旧字符串 findings 仍可读
   - closure-checker 对 structured findings 的 anti-hallucination / boundary checks 单测

3. **Patch readiness**
   - 构造“证据真实但不够指导 patch”的样例：closure 通过，readiness 失败
   - 构造“两个 req 各自正确但 patch_constraints 冲突”的样例：readiness 失败并指向 req ids

4. **Patch faithfulness**
   - diff-level 单测：签名漂移、alias 兼容层、过度测试改写三类反模式都能命中
   - patch-generator reflection 样例：Round 1 生成 patch 后，Round 2 能发现 forbidden_changes 并回退为 PATCH_INCOMPLETE

5. **端到端回归：issue002**
   - patch 不得擅改 `hide_qt_warning` / `QtWarningFilter` 的公开签名
   - 迁移题必须改真实调用点，而不是在旧模块留下 alias 当成完成迁移
   - 测试改动应以 relocation / minimal adjustment 为主，不应整块重写测试结构

6. **端到端回归：phase18 核心实例**
   - 重跑 issue001 / issue002，确保新增 readiness / faithfulness gate 不会破坏已有可收敛路径
   - 总体预算与时长仍在可接受范围内（新增 gate 后不应出现 uncontrolled tool explosion）

---

**非目标 / 开放问题**

- **不在 phase19 引入全新的外部 verifier 服务。** 优先在现有 orchestrator + agents 内完成 readiness / faithfulness 分层，避免再造一条并行流水线。
- **不把 patch success 简化为跑更多测试。** phase19 关注的是“补丁是否忠实落实 evidence”，不是单纯扩大测试执行覆盖率。
- **不立即做跨实例统计学习或自动调参。** 先把单实例 deterministic gates 建起来，再考虑统计层面的 heuristics。
- **如果 structured findings 过渡成本过高，可先双轨制落地。** 即新增结构化字段并保留旧字符串 findings 一个 phase，待 patch-planner / closure-checker 全部迁移后再删除旧字段。
