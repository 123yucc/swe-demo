# Phase 23: After 6 Cases — File-Oriented Implementation Checklist

## Goal
把前 6 个 case 暴露出的结构性问题，落成到具体文件与具体检查项：优先补足 Evidence card 的“可编码充分性”、新增真正的 Consistency Guard、稳定 evidence 更新机制、让 Patch Plan 显式承接 requirement-level contract，并把 semantic check 前移，而不是主要依赖 closure-checker 事后兜底。

---

## 1. Evidence Card Schema 与更新机制

### 1.1 `src/models/report.py`
- [ ] 给 requirement 级结构新增可编码字段：`trigger_conditions`、`expected_behaviors`、`edge_cases`、`key_outputs`、`status_codes`、`response_schema_points`、`default_value_constraints`
- [ ] 给 requirement 级结构新增 `behavioral_constraints`、`semantic_boundaries`
- [ ] 增加 evidence 版本元数据：`version`、`previous_version`、`updated_from_round`
- [ ] 增加冲突/替代记录：`contradictions`、`superseded_items`
- [ ] 增加 sticky 字段：`sticky_constraints`
- [ ] 确保新字段能被序列化到 evidence card、working memory、后续 patch plan

### 1.2 `src/agents/deep_search_agent.py`
- [ ] 改写 deep-search 输出要求：每个 requirement 不能只给 verdict/findings，必须显式提取“触发条件 + 预期行为 + 关键返回值/状态码/schema/default”
- [ ] 如果 failing test 名中出现 empty/null/falsy/not found/forbidden/success/default/override/fallback 等词，要求 deep-search 把这些词映射到对应 requirement 字段
- [ ] 如果测试名或 issue 描述显式包含反例/边界条件，要求落到 `edge_cases`，而不是只写在自由文本 findings
- [ ] 新增“同一 endpoint / function 多 requirement”时的 decision-table 输出要求，至少写明条件、分支、结果
- [ ] 输出更新时不要整卡覆盖，改为“基于上一版 evidence 的增量更新输入”

### 1.3 `src/orchestrator/engine.py`
- [ ] 实现 evidence merge-with-audit：高价值字段 `behavioral_constraints`、`semantic_boundaries`、`sticky_constraints` 默认合并，不允许被无理由删除
- [ ] 保存 previous evidence version，支持在 working memory 中追踪每一轮 evidence 演化
- [ ] 若新 evidence 与旧 evidence 冲突，不直接覆盖，改为记录到 `contradictions` 并在 rework 中显式要求澄清
- [ ] 若某字段被替代，写入 `superseded_items`，说明由哪个 requirement / round 替代
- [ ] 对 sticky constraints 增加守卫：一旦识别出 empty/falsy、permission gating、default/fallback、schema 等关键约束，后续轮次若删除必须附理由

---

## 2. Sufficiency：从“已表态”升级为“可编码充分”

### 2.1 `src/orchestrator/audit.py`
- [ ] 新增 `evidence_sufficiency_check` 的细粒度规则，不再只检查“是否有 verdict / findings”
- [ ] 检查每个 failing test name 中的显式行为词，是否在 evidence 中至少映射到一个 requirement 或 findings
- [ ] 检查每个 requirement 是否同时具备：`trigger_conditions`、`expected_behaviors`
- [ ] 若测试或 issue 涉及边界条件，检查 `edge_cases` 是否已覆盖
- [ ] 检查关键行为是否落实为结构化字段：返回值、状态码、schema、default/fallback、权限判定
- [ ] 对 endpoint/protocol 类 requirement 增加专项 sufficiency：必须能回答“什么条件下返回什么响应结构/状态码”
- [ ] 对 default/override/fallback 类 requirement 增加专项 sufficiency：必须能回答“默认值来源、覆盖条件、最终生效值”

### 2.2 `src/orchestrator/engine.py`
- [ ] 把 sufficiency fail 的原因结构化回传给 deep-search，而不是只给笼统的“evidence missing”
- [ ] rework 指令中明确缺失的是：触发条件、预期行为、边界条件、关键输出，还是 default/permission/schema 语义
- [ ] 若 failing tests 中有词面行为未映射到任何 requirement，直接触发定向补证 rework

### 2.3 `src/tools/` 或 `src/orchestrator/` 新增辅助模块
- [ ] 新增一个 failing-test phrase extractor，抽取 test name 里的关键行为词：如 empty、falsy、not exist、forbidden、valid response、default、fallback
- [ ] 为 extractor 建立可扩展词表，避免只靠 closure-checker 事后语义兜底

---

## 3. Consistency Guard：在 closure-checker 之前做结构一致性门控

### 3.1 `src/orchestrator/audit.py`
- [ ] 新增 `cross_requirement_consistency_check`
- [ ] 检查 requirement 间是否覆盖完整状态机 / 决策表，而不是只覆盖单个 happy path
- [ ] 对同一 endpoint / function 的多 requirement，检查是否形成互斥且完备的条件集合
- [ ] 检查 must_co_edit_relations 是否与 dependency_propagation、requirements、patch plan edits 一致
- [ ] 检查参数传播链中默认值是否一致，避免上游 requirement 说一种 fallback，下游 plan/coder 又按另一种实现
- [ ] 对 success / forbidden / not found / invalid input 等多分支协议类需求，检查 evidence 是否形成完整 response matrix

### 3.2 `src/agents/deep_search_agent.py`
- [ ] 在 prompt 中要求：若同一函数存在多个 failing tests，必须显式汇总为 decision table / state table，而不是拆成相互独立的观察
- [ ] 若存在多分支权限逻辑，要求列出 guest/user/admin 或 equivalent roles 的差异条件
- [ ] 若存在 default/override/fallback chain，要求列出 source-of-truth 与覆盖优先级

### 3.3 `src/orchestrator/engine.py`
- [ ] 把 consistency guard 放在 patch planner 之前执行；未通过时优先 rework evidence，而不是直接生成 patch
- [ ] 为 consistency fail 区分子类：状态机不完整、条件集合不完备、co-edit 不一致、默认值链冲突

---

## 4. Semantic Check 前移，不完全留给 Closure Checker

### 4.1 `src/orchestrator/audit.py`
- [ ] 新增 pre-plan semantic checks，覆盖以下模式：
  - [ ] endpoint / protocol response schema
  - [ ] permission gating
  - [ ] default / override / fallback chain
  - [ ] empty / null / falsy edge cases
  - [ ] new interface / new route registration completeness
- [ ] 把这些检查设计成 evidence -> plan 前置门，而不是 patch 后审计
- [ ] 对 new interface / new route 额外检查挂载点、导出点、调用点是否成套

### 4.2 `src/agents/patch_planner_agent.py`（若 planner 文件名不同，则落到实际 planner 文件）
- [ ] planner prompt 中加入语义前置约束：生成 patch plan 前必须确认 response schema、permission gating、fallback chain 已被 requirement 明确覆盖
- [ ] 如果证据里缺少这些语义，不允许 planner 用“推测式补全”继续规划，而应回退请求补证

### 4.3 `src/agents/closure_checker_agent.py`
- [ ] 保留 closure-checker 的事后审计职责，但弱化其对前置缺证的兜底负担
- [ ] 将 closure-checker 的失败理由与前置 semantic checks 对齐，便于定位是“前面没拦住”还是“patch 真写坏了”

---

## 5. Patch Plan：显式承接 requirement-level contract

### 5.1 `src/models/report.py` 或 patch plan 对应 schema 文件
- [ ] 为 `FileEditPlan` 新增：`requirements_covered`
- [ ] 新增：`must_preserve_behaviors`
- [ ] 新增：`decision_table_rows`
- [ ] 新增：`api_contracts`
- [ ] 新增：`default_value_constraints`
- [ ] 若已有 related fields，统一命名并避免与 must_co_edit_relations 重复表达

### 5.2 `src/agents/patch_planner_agent.py`
- [ ] planner 必须把每个 edit 明确绑定到 requirement id，不能只给 filepath + rationale
- [ ] 对 endpoint / controller / request handler 类问题，planner 必须写出 success / forbidden / not found / invalid input 等分支由哪个 edit 承接
- [ ] 对 empty/falsy、None/True/False、default/fallback 等语义，planner 必须写入 `must_preserve_behaviors` 或 `default_value_constraints`
- [ ] 对新增接口、路由、导出符号，planner 必须在 `decision_table_rows` 或 `api_contracts` 里写出注册完整性要求
- [ ] 若 requirement 无法映射到任一 FileEditPlan，planner 应直接报 coverage gap，而不是静默丢失

### 5.3 `src/agents/coder_agent.py`（若名称不同则落到实际 coder 文件）
- [ ] coder 提示词读取新增 plan 字段，确保代码生成时显式遵守 `must_preserve_behaviors`、`api_contracts`、`default_value_constraints`
- [ ] 在生成补丁前做一次 requirement 覆盖自检：plan 中列出的 behaviors 是否都能在 patch 中找到承接点

### 5.4 `src/orchestrator/engine.py`
- [ ] 增加 patch plan coverage check：所有 failing-test 映射到的关键行为词，都必须至少被一个 FileEditPlan 承接
- [ ] 若 coverage 不足，回退 planner rework，而不是交给 coder 碰运气补齐

---

## 6. Correct Attribution：保留，但不要一枝独大

### 6.1 `src/orchestrator/audit.py`
- [ ] 保留当前 evidence location / cited region 校验
- [ ] 增加弱语义邻近检查：findings 指向的对象、字段、返回结构，是否真的靠近 cited code region
- [ ] 区分“位置不对”与“位置对但解释偏表象”，避免所有失败都归为 attribution
- [ ] 调整审计顺序：先 sufficiency、再 consistency、再 semantic pre-check，最后才是 attribution 精修

### 6.2 `src/orchestrator/engine.py`
- [ ] rework feedback 要能区分：证据不足、证据冲突、语义不完整、attribution 偏移
- [ ] 避免把 consistency / sufficiency 问题统一反馈成“找错位置”

---

## 7. Rework Feedback 与轮次控制

### 7.1 `src/orchestrator/engine.py`
- [ ] 细分 rework reason code：`SUFFICIENCY_MISSING`、`CONSISTENCY_GAP`、`SEMANTIC_PRECHECK_FAIL`、`ATTRIBUTION_DRIFT`、`PATCH_PLAN_COVERAGE_GAP`
- [ ] deep-search rework 模板按 reason code 拆分，不再使用单一的 evidence_missing 指令
- [ ] planner rework 模板按 coverage gap / contract drop / co-edit inconsistency 拆分
- [ ] 若同一 sticky constraint 连续两轮被删除，升级为高优先级警报并阻止继续前进

---

## 8. Parser / Deep Search 相关落地点

### 8.1 `eval/SWE-bench_Pro-os/run_scripts/common_parser.py`（若这里负责结果抽取）
- [ ] 检查 parser 是否稳定抽取 failing tests 的完整名称与关键信号词
- [ ] 若 parser 当前只抽 test id，不保留行为短语，则补充结构化字段供主流程使用

### 8.2 `src/agents/deep_search_agent.py`
- [ ] deep-search 读取 parser 产出的 failing-test phrase signals，并强制映射到 requirement 结构字段
- [ ] 避免 deep-search 只围绕 issue 文本做 attribution，而忽略测试名暴露的关键语义

---

## 9. 验证与回归

### 9.1 新增/更新测试
- [ ] 为 sufficiency check 增加单测：覆盖 empty/falsy、not found / forbidden / success、default/fallback、schema 响应
- [ ] 为 consistency guard 增加单测：覆盖多 requirement 的决策表完备性、co-edit relation 一致性、默认值传播一致性
- [ ] 为 evidence merge-with-audit 增加单测：覆盖 previous version、contradiction、superseded、sticky constraint 不可无故丢失
- [ ] 为 patch plan schema 增加单测：确保 `requirements_covered`、`must_preserve_behaviors`、`decision_table_rows`、`api_contracts`、`default_value_constraints` 被实际消费

### 9.2 6 个 case 回归
- [ ] 先 rerun 3 个失败 case，重点观察：
  - [ ] NodeBB-04998908：empty/falsy 语义是否进入 requirement 和 patch plan
  - [ ] NodeBB-51d8f3b：webfinger 的 not found / forbidden / success 是否形成完整 decision table
  - [ ] ansible-a26c325：default/fallback / None 相关语义是否被 planner 承接
- [ ] 再 rerun 3 个已通过 case，确认新增 guard 不会误伤已有成功路径

---

## 10. 建议实施顺序（MVP）
1. `src/models/report.py`：先补 schema 字段
2. `src/agents/deep_search_agent.py`：输出 requirement-level contract
3. `src/orchestrator/audit.py`：先落 sufficiency + consistency + semantic pre-check
4. `src/orchestrator/engine.py`：接 evidence merge、reason code、rework feedback
5. `src/agents/patch_planner_agent.py` / `src/agents/coder_agent.py`：承接新 contract 字段
6. parser 与回归测试：验证 6 个 case
