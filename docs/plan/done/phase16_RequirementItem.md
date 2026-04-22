## Plan: Phase 16 — 引入 RequirementItem[] 并按职责收归证据卡写权限

**背景与根因**

phase15 后在 NodeBB email validation 实例（issue_001）真实评测失败，定位到三层根因：

1. Budget 耗尽后状态循环卡死。[src/orchestrator/engine.py:235](src/orchestrator/engine.py#L235) 的条件 `if missing and not budget.is_exhausted()` 使得预算耗尽后 EVIDENCE_REFINING 不再回流 UNDER_SPECIFIED 触发 deep-search，但 closure-checker 仍被反复调用。实跑中 deep-search 只跑了 5 次，closure-checker 却被调 32 次；最终 CLOSURE_APPROVED 是 LLM 对同一份未变化证据反复采样时的输出噪声通过，不是证据真的满足闭环。
2. Parser 把 requirements 原样塞进 `constraint.behavioral_constraints` 并加 "TO-BE: " 前缀（[src/agents/parser_agent.py:51-54](src/agents/parser_agent.py#L51-L54)）；但 deep-search 的 update_localization schema 不接受这个字段（[src/tools/ingestion_tools.py:220-228](src/tools/ingestion_tools.py#L220-L228)），而 closure-checker 又判 TO-BE 不算证据（[src/agents/closure_checker_agent.py:29](src/agents/closure_checker_agent.py#L29)）——字段从一出场就死锁。
3. `update_localization` 合并策略是纯字符串 dedup + 按 file:line 的最长保留（[src/tools/ingestion_tools.py:207-215](src/tools/ingestion_tools.py#L207-L215)），累积 5 轮后 suspect_entities 堆到 ~40 条，出现大量"BUG / NOT A BUG / ALREADY IMPLEMENTED"互相矛盾的断言。closure-checker 的 Consistency 判定无法通过。

三层根因叠加导致：patch 只覆盖 requirements 13 条里的 3 条（null-check 和反转逻辑 bug），requirements 明确要求的 `canSendValidation` 间隔检查、`isValidationPending` 三重条件严格化等全部遗漏。

设计哲学：证据闭环三要素（Sufficiency / Consistency / Correct attribution）需要显式的、机械化可判定的度量，不能全靠 closure-checker 主观判断。

**Steps**

1. Phase 16.A — 修 budget 耗尽死循环（独立可回滚，最先做）
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 引入 `_forced_closure_done: bool` 本地标志。当 `budget.is_exhausted()` 且尚未 forced 时，允许进入 EVIDENCE_REFINING 强制调一次 closure-checker；无论结果 APPROVED 还是 EVIDENCE_MISSING，立刻跳出主循环。
- MISSING 路径下将 state 设为新增终态 `CLOSURE_FORCED_FAIL`（见 16.A 同步改 states.py），跳过 PATCH_PLANNING/PATCH_GENERATOR，patch_outcome 记为 `"EVIDENCE_INCOMPLETE"`。
- 在 [src/orchestrator/states.py](src/orchestrator/states.py) 的 PipelineState 增加 CLOSURE_FORCED_FAIL 终态并加入 ALLOWED_TRANSITIONS。
- 依赖：无。
- 验证：人为把 `DeepSearchBudget(max_iterations=1)` 跑 NodeBB 实例，closure-checker 调用次数 ≤ 2。

2. Phase 16.B — 新增 RequirementItem 数据模型 + schema_version
- 在 [src/models/evidence.py](src/models/evidence.py) 新增 `RequirementItem` pydantic 模型，字段：
  - `id: str` — 格式 `req-001`、`req-002`……
  - `text: str` — 原始 requirement 文本（不截断）
  - `origin: Literal["problem_statement", "requirements", "new_interfaces"]`
  - `verdict: Literal["UNCHECKED", "AS_IS_COMPLIANT", "AS_IS_VIOLATED", "TO_BE_MISSING", "TO_BE_PARTIAL"]`（默认 `UNCHECKED`）
  - `evidence_locations: list[str]` — `file:line-line` 格式；AS_IS_COMPLIANT 可为空，其他 verdict 必须非空
  - `findings: str` — deep-search 的现场核查结论，便于下游 patch agent 复用
- 在 [src/models/context.py](src/models/context.py) 的 EvidenceCards 增加：
  - `requirements: list[RequirementItem]`
  - `schema_version: Literal["v2"]`（默认 `"v2"`，phase16 前的为隐式 v1）
- `localization.*` 字段本期保留（作为从 RequirementItem 聚合的只读视图），不再由 deep-search 直接写入；是否最终废弃另议。
- 依赖：无。

3. Phase 16.C — Parser 精简 + 输出 RequirementItem[]
- 重写 [src/agents/parser_agent.py](src/agents/parser_agent.py) 的 PARSER_SYSTEM_PROMPT，目标 ≤ 30 行：
  - 删除所有 AS-IS/TO-BE 前缀约定（字段语义由字段名承载，不用前缀重复标注）
  - 每字段一行定义
  - 明确 parser 只产出三类内容：
    * `symptom.observable_failures / repair_targets / regression_expectations` ← 从 problem_statement 提取
    * `constraint.missing_elements_to_implement` ← 仅从 "New interfaces introduced:" 段提取新接口签名
    * `requirements: RequirementItem[]` ← 从 "Requirements:" 段逐条拆分，每条一个 item，初始 `verdict=UNCHECKED`、`origin="requirements"`
  - parser 输出的所有其他字段一律空列表（`constraint.behavioral_constraints / semantic_boundaries / backward_compatibility / similar_implementation_patterns / localization.* / structural.*` 全部留空，交由 deep-search 填写）
- 依赖：16.B。
- 验证：NodeBB 实例的 parser 输出 requirements 条数 ≥ 10（与原始 "Requirements:" 段条数一致）。

4. Phase 16.D — Deep-search 按 RequirementItem 逐条核查 + prompt 精简
- [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 TODO 构造改为：遍历 `requirements` 里所有 `verdict==UNCHECKED` 的条目，每轮 deep-search 取 1 条（或 N 条，但单轮只给一个显式 requirement_id scope）
- [src/models/report.py](src/models/report.py) 的 DeepSearchReport 增加字段：
  - `target_requirement_id: str` — 本轮核查的 req id
  - `requirement_verdict: Literal[...]` — 得出的结论
  - `requirement_findings: str`
  - `requirement_evidence_locations: list[str]`
  - 保留原有 localization/structural 字段（这些是 AS-IS 代码结构观察，独立于 requirement verdict）
- [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) 的 DEEP_SEARCH_SYSTEM_PROMPT 重写并大幅精简（目标 ≤ 15 行）：
  - 删除 L26-L34 大段 AS-IS / TO-BE 反复说明（requirement scope 已显式化，不需要在 prompt 里再重复）
  - 明确告诉模型本次任务只聚焦一条 requirement，对其做 verdict 判定并给出证据位置
  - 只保留关键工具用法（Grep/Read/Glob）和路径规范（相对 repo root）
- 依赖：16.B、16.C。

5. Phase 16.E — update_localization 改为按 scope 替换，取消 merge
- [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) 的 update_localization schema 必填参数增加 `scope_requirement_id: str`
- 写入逻辑改为：每个 localization/structural 条目内部结构由 `list[str]` 改为 `dict[scope_id, list[str]]`（或给 entry 加 `[req-xxx]` 前缀，便于按 scope 整体替换）
- 新 scope 的写入替换该 scope 的旧条目，不再 append
- 聚合视图：对外读接口 `get_submitted_evidence()` 仍返回 `list[str]`（按 scope 扁平化聚合），保持 parser / closure-checker 调用处不变
- 删除 `_merge`、`_dedup_by_location`、contradiction guard 三段代码（scope-based 替换后不再必要）
- schema 扩充：允许在 scope 条目里携带 `similar_pattern: str` 字段，专门存 deep-search 发现的参考实现。写回到 `constraint.similar_implementation_patterns` 时同样按 scope 替换，避免另起一套写入路径
- 新增 `update_requirement_verdict(requirement_id, verdict, evidence_locations, findings)` 工具供 deep-search 调用，专用于 RequirementItem 更新
- 依赖：16.B、16.D。
- 验证：连续两轮对同一 req-id 调 deep-search，第二轮结果应完全替换第一轮在该 scope 下的 localization 条目。

6. Phase 16.F — Closure-checker 机械化三要素判定 + 删除遗留机械检查
- 在 [src/orchestrator/guards.py](src/orchestrator/guards.py) 新增 `check_sufficiency(requirements)` 和 `check_correct_attribution(requirements)` 两个机械闸门：
  - Sufficiency：`all(r.verdict != "UNCHECKED" for r in requirements)`
  - Correct attribution：`all(r.verdict == "AS_IS_COMPLIANT" or r.evidence_locations for r in requirements)`
- orchestrator 在调 closure-checker 之前先跑两个机械闸门；任一失败直接退回 UNDER_SPECIFIED 并生成定向 TODO，不调 LLM
- 两个机械闸门都通过后才调 closure-checker，它只负责 Consistency（语义冲突）的最终判定
- 改写 [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 的 system prompt：删除 AS-IS/TO-BE、"empty fields fail"、"mechanical checks already enforced" 等与机械闸门重复的指令，只保留"检查同一 evidence_location 是否在不同 requirement 间结论冲突"这一维度
- 删除 [src/orchestrator/guards.py](src/orchestrator/guards.py) 里的遗留死代码 `check_mechanical_closure` 和 `check_evidence_format`：新范式下 Sufficiency 由 requirement verdict 覆盖度承担，`exact_code_regions` 格式由 `RequirementItem.evidence_locations` 的 pydantic 校验承担，两者均不再需要
- 连带删除 [src/orchestrator/engine.py](src/orchestrator/engine.py) 里对这两个函数的调用点
- 依赖：16.B、16.C、16.D。

7. Phase 16.G — 字段写权限按职责表收归
- 在 [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) 的 update_localization 内部加白名单校验；deep-search 误写 symptom.* 或 constraint.missing_elements_to_implement 时返回 ERROR，不静默接受
- Parser 的 pydantic 校验也收紧：要求 Parser 输出的 localization/structural 字段为空、constraint.behavioral_constraints 等由 deep-search 维护的字段为空
- 字段 → 写入者映射表（见 Decisions）由代码强制
- 依赖：16.B、16.E。

8. Phase 16.H — 旧工件迁移与加载防御
- 清理 [workdir/swe_issue_001/outputs/](workdir/swe_issue_001/outputs/) 下残留的旧 schema 产物（evidence_cards.json / patch_plan.json / working_memory.json），至少做一次备份再删除
- 更新 [src/main.py](src/main.py) 在加载已存在的 evidence_cards.json 时检查 `schema_version`：缺失或非 `v2` 直接报错退出，不做自动迁移（避免隐式兼容导致的新 bug）
- 同步更新 [CLAUDE.md](CLAUDE.md) 的架构说明段与字段写权限段，避免文档与代码不一致
- 在 [CLAUDE.md](CLAUDE.md) 追加"SDK 对齐决策记录"小节，明确以下偏离是有意的而非疏漏：
  * 不使用 SDK 的 `agents={}` + `AgentDefinition` + `Agent` 工具机制做子 agent 分派 — 因为 SDK subagent 返回值只有 final assistant message 字符串，无法拿到 Pydantic 校验过的结构化对象；我们改用独立 `query()` + `output_format` 每轮产出强类型结果
  * `action_history` 只承载跨 agent 的聚合维度；单次 query 内部的逐条 message 明细依赖 SDK 自动写入 `~/.claude/projects/<encoded-cwd>/*.jsonl`，不做二次存储
  * `DeepSearchBudget` 是 pipeline 级（跨多次 query）、SDK `max_turns`/`max_budget_usd` 是单次 query 级，两者互补而非重复
- 依赖：16.B、16.G。

9. Phase 16.I — action_history 结构化（收窄范围，低优先级）
- [src/models/memory.py](src/models/memory.py) 的 `action_history: list[str]` 改为 `list[ActionEvent]`，`ActionEvent` 只保留聚合维度：`phase / subagent / outcome / requirement_id`
- 不再记录 `elapsed_ms / detail / message 内容` — 这些由 Claude Agent SDK 自动持久化到 `~/.claude/projects/<encoded-cwd>/*.jsonl` 中，调用 `claude_agent_sdk.list_sessions()` / `get_session_messages()` 可获取
- `SharedWorkingMemory.record_action()` 改为结构化入参
- 调用点（orchestrator、update_localization、update_requirement_verdict）改为写入结构化事件
- 好处：可以机械统计各 requirement 的 verdict 演化过程，便于未来问题归因；避免与 SDK session 文件重复存储
- 依赖：无强依赖，但建议在 16.A-16.G 落地后再做，避免同时改动太多

10. Phase 16.J — 子 agent 单次调用级别的 tokens/cost 兜底
- 给所有 `query()` 调用点（parser / deep-search / closure-checker / patch-planner / patch-generator）的 `ClaudeAgentOptions` 补上 `max_turns` 和 `max_budget_usd`，作为 SDK 单次查询级别的硬兜底
- 建议初始值：deep-search `max_turns=30, max_budget_usd=1.0`；parser/closure-checker/patch-planner `max_turns=10, max_budget_usd=0.3`；patch-generator `max_turns=40, max_budget_usd=1.5`
- 与 pipeline 级的 `DeepSearchBudget`（跨多次 query 调用）互补：SDK 的两个限制是单次 query 内部 tool-use 轮数/花费，`DeepSearchBudget` 是 orchestrator 调用 deep-search 的次数
- 需在 `ResultMessage.subtype == "error_max_turns" | "error_max_budget_usd"` 分支里显式处理，写入 action_history 并退回 UNDER_SPECIFIED（对应子 agent）或 PATCH_FAILED（patch 侧）
- 依赖：无，可与任何阶段并行落地

**Relevant files**

- [src/orchestrator/engine.py](src/orchestrator/engine.py) — 主循环、budget 耗尽兜底、TODO 构造改为按 RequirementItem 驱动
- [src/orchestrator/guards.py](src/orchestrator/guards.py) — 新增 Sufficiency / Correct attribution 机械闸门；删除 check_mechanical_closure / check_evidence_format
- [src/orchestrator/states.py](src/orchestrator/states.py) — 新增 CLOSURE_FORCED_FAIL 终态
- [src/agents/parser_agent.py](src/agents/parser_agent.py) — prompt 精简 ≤ 30 行，输出 RequirementItem[]
- [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) — 按 requirement scope 单条核查；prompt 精简 ≤ 15 行
- [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) — 只判 Consistency，prompt 大幅收缩
- [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) — scope-based 替换、新增 update_requirement_verdict、携带 similar_pattern 字段、删除 _merge / _dedup_by_location / contradiction guard、加字段写权限白名单
- [src/models/evidence.py](src/models/evidence.py) — 新增 RequirementItem
- [src/models/context.py](src/models/context.py) — EvidenceCards 增加 requirements 与 schema_version 字段
- [src/models/report.py](src/models/report.py) — DeepSearchReport 增加 target_requirement_id / requirement_verdict / requirement_findings / requirement_evidence_locations
- [src/models/memory.py](src/models/memory.py) — action_history 结构化（16.I）
- [src/main.py](src/main.py) — schema_version 加载校验（16.H）
- [CLAUDE.md](CLAUDE.md) — 同步架构与字段写权限表（16.H）
- [workdir/swe_issue_001/outputs/](workdir/swe_issue_001/outputs/) — 旧工件清理对象

**Decisions**

- 字段 → 写入者映射：

  | 字段 | 写入者 |
  |---|---|
  | `symptom.*` | Parser only（issue 给的，deep-search 不该动）|
  | `constraint.missing_elements_to_implement` | Parser only（从 "New interfaces introduced:" 提取）|
  | `requirements: list[RequirementItem]` | Parser 初始化（verdict=UNCHECKED）；Deep-search 通过 `update_requirement_verdict` 更新 verdict / evidence_locations / findings |
  | `localization.*`、`structural.*` | Deep-search |
  | `constraint.behavioral_constraints / semantic_boundaries / backward_compatibility / similar_implementation_patterns` | Deep-search（从代码里读出的 AS-IS 约束）|

- 三要素机械化分工：
  - Sufficiency：代码判（`check_sufficiency`）
  - Correct attribution：代码判（`check_correct_attribution`）
  - Consistency：LLM 判（唯一保留语义判断的维度）

- 字段语义重定义：
  - `constraint.behavioral_constraints` 从"TO-BE 需求堆栈"改为"从代码里读出的 AS-IS 行为约束"
  - 废止 "TO-BE: " 前缀约定——字段名本身已承载 AS-IS/TO-BE 语义，不再额外标注
  - TO-BE 统一由 `requirements: list[RequirementItem]` 承载，verdict 字段表达核查状态

- 执行顺序：16.A（独立兜底）→ 16.B（数据模型，底座）→ 16.C（Parser）→ 16.D（Deep-search）→ 16.E（写入路径）→ 16.F（闭环判定）→ 16.G（权限收归）→ 16.H（工件迁移）→ 16.I（action_history 结构化，低优先级）→ 16.J（单次 query 级 tokens/cost 兜底，可并行）。16.A 可与 16.B 并行；16.J 可与 16.A-16.H 任意阶段并行。

- RequirementItem 与证据卡片的职责分工（避免 patch 阶段漏信息）：
  - `RequirementItem[]` 是任务驱动单元，patch_planner 的主索引
  - 证据卡片其他字段是全局上下文切片，patch 阶段仍需读取：
    * `symptom.regression_expectations` — patch_generator 判断"不能破坏什么"
    * `symptom.repair_targets` — 全局修复愿景
    * `structural.must_co_edit_relations` — 跨 requirement 的协同改动（不属于任一条 requirement）
    * `constraint.behavioral_constraints / semantic_boundaries / backward_compatibility` — 全局 AS-IS 红线
    * `constraint.similar_implementation_patterns` — 参考实现
    * `constraint.missing_elements_to_implement` — 来自 "New interfaces introduced:"，独立于 Requirements 段
  - `localization.*` 在新架构下基本被 RequirementItem.evidence_locations 吞并，本期保留为聚合视图，是否废弃留到后续 phase 决定

**Verification**

1. Budget 死循环 smoke test：`DeepSearchBudget(max_iterations=1)` 运行 NodeBB 实例，closure-checker 调用次数 ≤ 2；action_history 不再出现连续 EVIDENCE_MISSING 空转。
2. RequirementItem 覆盖度：NodeBB 实例 parser 产出 `len(evidence.requirements) ≥ 10`，且每条的 origin 正确归属。
3. Deep-search scope 隔离：人为连跑两次 `verify req-005`，第二次返回后 evidence_cards 里 req-005 关联的 localization 完全是第二次的结果，第一次的条目不应残留。
4. 字段写权限：deep-search 尝试写 `symptom.observable_failures` 时 update_localization 返回 ERROR；测试覆盖 symptom 与 missing_elements_to_implement 两类禁写字段。
5. 机械闸门：构造一条 verdict==UNCHECKED 的 requirement，直接跳过 deep-search 调 orchestrator，应在机械闸门层退回 UNDER_SPECIFIED 而非进入 closure-checker。
6. 端到端回归：NodeBB 实例重跑，patch 应覆盖 requirements 中至少 5 条以上（含 `canSendValidation` 间隔检查、`isValidationPending` 三重条件严格化等当前遗漏项）。
7. Schema 加载防御：人为拿旧 schema 的 evidence_cards.json 跑 [src/main.py](src/main.py)，应在加载阶段明确报错而非静默进入 phase1。
8. 旧机械检查死代码清零：全仓 grep `check_mechanical_closure` 和 `check_evidence_format` 应无命中。

---

**后续修订**：phase16 设计里"closure-checker 只判 Consistency（纯字符串比对）"这一条已在 phase17 升级为"带 repo 访问的审计型闸门 + 机械 gate 仅做格式检查"。详见 [docs/plan/phase17_closure_audit.md](../phase17_closure_audit.md)。
