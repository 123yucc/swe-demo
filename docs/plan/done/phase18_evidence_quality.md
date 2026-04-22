## Plan: Phase 18 — Evidence 质量的确定性管控（代码审计清单 + 字段间不变量 + 约束传递 + 弱环节反省）

**背景与根因**

phase17 端到端跑通到 PatchSuccess（18.4 min，vs phase16 40+ min 未完成），但 NodeBB instance_001 真实评测 3 条测试失败。顺着失败链路回溯 evidence.json：

1. **req-014（`db.mget` new_interface）verdict=AS_IS_COMPLIANT**，findings 原文：
   > "All three database adapters (Redis, MongoDB, PostgreSQL) fully implement db.mget as specified. Each adapter exports a `module.mget = async function (keys)` method..."

   完全是**幻觉**。代码里根本没 `db.mget`，模型把 `db.getObjects` / `client.mGet` 等近义方法当成对 `db.mget` 的证明。phase17 closure-checker 按 17.C 焦点机制跳过了"AS_IS_COMPLIANT 且无 overlap"的条目 → 幻觉直通 patch 阶段 → patch 没碰 `src/database/{mongo,postgres,redis}/main.js` → 两条 `db.mget` 测试失败。

2. **req-013（`canSendValidation` interval check）verdict=AS_IS_VIOLATED**，findings 给出了 prescriptive 修复方案："The correct comparison is `(ttl || Date.now()) + interval > max`"，但同一段 findings 下一句自我反证：
   > "When ttl is small/near-zero ..., sum < max → false → **which is WRONG for expired confirmations**. The operator must be flipped..."

   模型自承翻转在 expired case 下不对，但仍规定翻转。patch-generator 忠实执行 → 得到同样错的公式 → canSendValidation 测试失败。

3. **req-008（`delete.js` 应调 `expireValidation`）AS_IS_COMPLIANT 假阳性**，patch 没碰 `src/user/delete.js`。

**根因分层**

| 层 | 问题 | phase17 的应对 | 为何失败 |
|---|---|---|---|
| 审计集选择 | closure-checker 的"审哪些 req"由 prompt 规则决定 | 17.C 在 system prompt 里写焦点规则 | 长上下文下规则可被 LLM 静默忽略；且规则写死"AS_IS_COMPLIANT 无 overlap 跳过"恰好放过了 req-008/014 这类假 compliant |
| 证据语义一致性 | evidence 各字段间 / 字段内可有语义矛盾（findings vs verdict、prescriptive vs 边界、new_interfaces vs missing_elements） | 17.C 判"verdict vs 代码"但不判字段间矛盾 | 矛盾在语义层而非字面，代码 gate 未触及，LLM 审计未覆盖 |
| 约束传递到 PatchPlan | patch-planner 把 findings 摘要为 FileEditPlan.description，丢失 prescriptive 原文片段 | 无 | patch-generator 只看 PatchPlan 摘要，不读原 finding，关键边界约束丢失 |
| 弱环节无自省 | deep-search 一次过产出 findings 后不自查 | 无 | 幻觉 / 自相矛盾的 prescriptive 无人拦截就上 closure-checker |

设计哲学（承接 phase16/17）：

- **审计决策权要从 LLM prompt 规则回收到代码**。LLM 只负责"按任务表做审计"，不负责"判断哪些该审"。
- **确定性 gate 可判的一律前移到代码**：字段间结构不变量、interface-name 交叉匹配等都属于机械可判。
- **约束必须跟着上下文走到下游**：PatchPlan 要保留 prescriptive findings 的原文片段，patch-generator 要直达 evidence。
- **弱环节加自省**比外部兜底更经济：同一 agent 同一上下文的 reflection 成本低，能拦截 findings 幻觉。

---

**Steps**

### 18.A — 字段间结构不变量（代码侧机械 gate）

四张证据卡片 + RequirementItem[] 之间存在**结构必然性**：parser 抽的 missing_elements 必须有对应 new_interfaces requirement；反之 new_interfaces 的 req 也必须在 missing_elements 里找得到名字呼应；symptom.observable_failures 至少要能映射到一条 requirements-origin 的 req。这些都是字面级可判，前移到代码层避免后续 LLM 审计漏判。

- 在 [src/orchestrator/guards.py](src/orchestrator/guards.py) 新增 `check_structural_invariants(evidence) -> list[str]`，返回违反的条目描述列表：
  - **I1 (new_interface ↔ missing_elements 双向映射)**：
    - 对每条 `origin=="new_interfaces"` 的 req，抽取其接口名（文本里 "Name: X" 之后的第一个标识符 token）；该名必须在 `constraint.missing_elements_to_implement` 的任一行中出现，否则记 `"I1_orphan_new_interface_req: <rid> name=<name>"`
    - 对每行 `missing_elements_to_implement`，抽取首个接口名；该名必须对应至少一条 `origin=="new_interfaces"` 的 req，否则记 `"I1_orphan_missing_element: <line>"`
  - **I2 (new_interface compliant 必矛盾)**：任一 `origin=="new_interfaces"` 且 `verdict=="AS_IS_COMPLIANT"` 的 req → 记 `"I2_new_interface_cannot_be_compliant: <rid>"`（按定义新接口不可能已存在；compliant 意味着 deep-search 幻觉）
  - **I3 (symptom → requirements 覆盖)**：每条 `symptom.observable_failures` 应能与至少一条 `origin=="requirements"` 的 req 文本做弱关键词匹配（共享 ≥ 2 个非停用词），否则记 `"I3_orphan_symptom: <failure>"`（提示 parser 抽取不一致 / requirement 覆盖不全）
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) EVIDENCE_REFINING 分支、`check_correct_attribution` 之后、`build_audit_manifest`（见 18.B）之前调用此 gate：
  - I2 命中 → 直接 reset 该 req 为 UNCHECKED，audit_feedback="按 parser 标注这是需新增接口，不应已存在；请重新核验"，回 UNDER_SPECIFIED
  - I1 / I3 命中 → 记入 action_history，作为 warning 追加到 closure-checker 的输入末尾；不阻断流程（这两类可能是 parser 抽取不完美，不必触发返工，但要让审计阶段知道）
- 依赖：无。
- 验证：单测 6 类场景（I1 两向、I2、I3，各构造命中 / 不命中）+ 用真实 phase17 evidence 喂入，I2 必须在 req-014 命中。

### 18.B — 代码侧审计清单（AuditManifest）取代 prompt 焦点规则

phase17 把审计集选择写在 closure-checker 的 system prompt 里，相当于一组"软规则"。长上下文 + MiniMax 对指令理解漂移 → 规则常被静默忽略（phase17 跑的 evidence 里 req-014 就是这样逃过审计的）。改为**由 engine 代码在每次调 closure-checker 前先计算确定性 `AuditManifest`**，把"要审哪些、每条审什么"作为结构化输入喂进去，closure-checker 只能产出对应结构化结果，engine 再验收。

- 在 [src/models/verdict.py](src/models/verdict.py) 或新文件 `src/models/audit.py` 新增：
  ```python
  class AuditTask(BaseModel):
      requirement_id: str
      reasons: list[str]          # 如 ["new_interface_compliant_flagged", "non_compliant_defect",
                                  #     "overlap_group", "findings_has_backtick_tokens",
                                  #     "findings_has_prescriptive"]
      cited_locations: list[str]
      checks_required: list[str]  # 子集 of {"verdict_vs_code",
                                  #          "findings_anti_hallucination",
                                  #          "prescriptive_boundary_self_check"}

  class AuditResult(BaseModel):
      requirement_id: str
      per_check: dict[str, Literal["PASS", "FAIL", "SKIPPED"]]
      evidence_opened: list[str]       # closure-checker 实际 Read 了哪些 file:range
      failures: list[str]              # 每个 FAIL 的具体说明
  ```
- 修改 [src/models/verdict.py](src/models/verdict.py) 的 `ClosureVerdict`，新增 `audited: list[AuditResult]` 字段。
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 新增 `build_audit_manifest(evidence: EvidenceCards) -> list[AuditTask]`，规则**全部代码判定**：
  - 非 AS_IS_COMPLIANT 的 req → 加 AuditTask，checks=["verdict_vs_code", "findings_anti_hallucination", "prescriptive_boundary_self_check"]
  - `origin=="new_interfaces"` 的 req 不论 verdict → 加 AuditTask，checks=["verdict_vs_code", "findings_anti_hallucination"]
  - evidence_locations 与其他 req 有位置重叠的 AS_IS_COMPLIANT req → 加 AuditTask，checks=["verdict_vs_code"]
  - findings 里出现反引号片段的 req → 在已有 task 上补 "findings_anti_hallucination" 检查
  - findings 里出现 prescriptive 关键词（"correct", "should be", "must be", "正确", "应改为"）的非 compliant req → 补 "prescriptive_boundary_self_check"
- 修改 [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py)：
  - prompt 改为"你将收到一份 AuditManifest，对每个 AuditTask 按 checks_required 执行对应检查并产出 AuditResult；不要审 manifest 之外的 requirement"
  - 用户消息把 manifest 作为 JSON 结构化注入
  - closure-checker 返回的 ClosureVerdict.audited 必须覆盖 manifest 里的每个 req_id
- engine 端收到结果后校验：
  - `{t.requirement_id for t in manifest} == {r.requirement_id for r in verdict.audited}` → 不相等即判此次 closure-checker 失败，等同 EVIDENCE_MISSING（漏审本身就是缺陷）
  - 任一 AuditResult.per_check 里有 FAIL → EVIDENCE_MISSING，对应 req 进入返工队列
- 依赖：无（与 18.A 互补，18.A 的 I2 命中通常会在 18.B 阶段就不会再被看到）。
- 验证：
  - 喂 phase17 实测 evidence → manifest 必须包含 req-008、req-014（即便它们是 compliant）
  - closure-checker 结果漏审（人工改返回）→ engine 判 EVIDENCE_MISSING
  - 正常情况下 manifest 大小 ≤ 实际需审条目数（AS_IS_COMPLIANT 且无理由的不进 manifest，保证成本可控）

### 18.C — 三类语义审计落地（在 manifest 驱动下）

18.B 把检查类型明确后，closure-checker 的 prompt 对每种 check 给一段精确指令：

- **verdict_vs_code**（phase17 已有，保留）：Read cited evidence_locations，判定该段代码的实际行为是否支撑该 verdict。
- **findings_anti_hallucination**（新）：findings 里反引号 / 缩进代码块包裹的片段是 requirement 对"代码实际存在"的断言；必须在 cited 文件的 Read 内容里用 Grep 或文本包含判定验证；找不到即 FAIL。
- **prescriptive_boundary_self_check**（新）：findings 含 prescriptive 修法（"correct form is ..."）；要求 closure-checker 对原 requirement 文本蕴含的行为，至少枚举 2 个边界条件（临界值 / null vs 非 null / 空集合 vs 非空），把 prescriptive 代入原代码场景演算各边界下的返回值 / 副作用，全部符合 requirement 期望才算 PASS；任一边界失败 → FAIL，在 failures 字段写出"边界 X 下 prescriptive 会产生 Y，与 requirement 描述的 Z 不符"。

- 在 [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 的 CLOSURE_CHECKER_SYSTEM_PROMPT 里为三类 check 各写一段精确指令，长度控制（单段 ≤ 8 行）以减少长上下文漂移。
- prescriptive 关键词 18.B 代码已预判过，manifest 会告诉 closure-checker "这条需做 boundary self-check"，LLM 只需执行。
- 依赖：18.B。
- 验证：构造 3 类各一个 minimal 样本，单测 closure-checker 的 AuditResult.per_check 输出。

### 18.D — 约束传递到 PatchPlan

Phase17 的 req-013 findings 含"correct formula: `(ttl || Date.now()) + interval > max`"，但最终 patch 是单纯把 `< max` 翻成 `> max`（实际上和 findings 写的公式一样，但都是错的）。即使 findings 正确，现结构也存在约束丢失风险：patch-planner 把 findings 摘要为 FileEditPlan.description，patch-generator 只看摘要。

- 修改 [src/models/patch.py](src/models/patch.py) 的 `FileEditPlan`：新增字段 `preserved_findings: list[str] = []`，描述为"patch-planner 必须把与本次 edit 相关的 RequirementItem.findings 里的 prescriptive 原文片段（反引号代码 / "correct form is" / 明确的边界约束）原文保留在此字段；禁止摘要化改写"。
- 修改 [src/agents/patch_planner_agent.py](src/agents/patch_planner_agent.py) 的 PATCH_PLANNER_SYSTEM_PROMPT：
  - 明确"对每条 FileEditPlan，扫描相关 RequirementItem.findings，把 prescriptive 片段原文 copy 到 preserved_findings"
  - 给一个正反例
- 修改 [src/agents/patch_generator_agent.py](src/agents/patch_generator_agent.py) 的 PATCH_GENERATOR_SYSTEM_PROMPT：
  - 把每条 FileEditPlan 的 preserved_findings 作为"hard constraint"在执行 SEARCH/REPLACE 前必须先复核是否匹配；不匹配则调整实现
  - 额外在 user prompt 里把原 evidence.requirements 对应条目原文一起喂入（而不是只给 PatchPlan 摘要），长度风险可控
- 依赖：无（独立于 A/B/C）。
- 验证：跑 phase17 那份 evidence → patch-planner 输出 PatchPlan.json，FileEditPlan.preserved_findings 必须非空且包含 "ttl" "interval" "max" 这类原文 token；patch-generator 的 prompt 被 dump 下来时必须含 evidence.requirements 原文。

### 18.E — Deep-search 自省轮（弱环节 reflection）

phase17 数据指向最大的误差源是 deep-search：幻觉 findings + 自相矛盾 prescriptive。在 `_run_deep_search_async` 的一次结构化输出后加一轮自省，比走外部 closure-checker 审计便宜且精确。

- 修改 [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py)：
  - 第一轮产出 `DeepSearchReport` 后，起第二轮 `_reflect_on_findings(report, evidence)`：
    - 输入：第一轮的 report（含 findings、evidence_locations、verdict）+ 一份"自省任务单"
    - 任务单两项：
      1. **token 回溯**：列出 findings 里所有反引号片段和类函数名，逐一自问"我本轮的 Read 工具实际打开过哪些文件、哪些片段是从 Read 返回内容里抄来的，哪些是我记忆或推断的？" 凡是无 Read 支撑的片段一律删除或改写
      2. **边界演算**：如果 verdict 是 AS_IS_VIOLATED / TO_BE_* 且 findings 含 prescriptive，枚举 ≥ 2 个边界条件代入候选修法，任一不通过即在 findings 里记录为 open-issue（而不是隐藏）
    - 返回修订版 DeepSearchReport
  - 新轮次也用 `run_structured_query`，`allowed_tools=["Grep","Read","Glob"]`（允许复查同一批文件）、`max_turns=10`、`max_budget_usd=0.5`
  - reflection 若在 1 轮内产出失败（`error_max_turns` 等），退回使用第一轮结果，只记 warning
- 依赖：无（独立，但与 18.A/B/C 互补：self-reflect 减少进入审计的缺陷量）。
- 验证：构造一份 forced-hallucination 场景（prompt 里给一个不存在的文件引用），观察 reflection 把幻觉 token 删除；跑 phase17 真实 evidence，对比 reflection 前后 findings 里反引号片段数量 / 文本是否收敛。

### 18.F — 返工 prompt 对新增审计类型分化

18.A/B/C 产生的新 audit feedback 有三类（I2 new_interface、anti_hallucination、prescriptive_boundary），phase17 的 `_build_per_req_audit_feedback` 把所有反馈塞进同一模板。按类分化能提高返工收敛率。

- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 `_build_per_req_audit_feedback` 根据 failures 文本前缀分流：
  - `new_interface_cannot_be_compliant` → "parser 标此接口为新增，你判 compliant。请重新开 cited 文件核验接口名 `<name>` 是否真的定义；若只找到近义函数（如 `getObjects` vs `mget`），verdict 必须改为 TO_BE_MISSING"
  - `findings_anti_hallucination` / findings 里引用的片段在 Read 内容外 → "上轮 findings 声称代码含 `<snippet>`，审计在 cited 范围内找不到。本轮 findings 只能引用你 Read 工具实际返回过的内容；禁止记忆或推断式复述"
  - `prescriptive_boundary_self_check` → "上轮 prescriptive fix 在边界 `<boundary>` 下行为违反 requirement；请先枚举 requirement 文本蕴含的至少 2 个边界条件，再决定 verdict + findings，每个边界都要显式给出 'pass/fail + why' 的一行记录"
- 依赖：18.A/B/C。
- 验证：三类各构造一次，看下一轮 deep-search user prompt 打印里对应分化段。

### 18.G — docs + 回归

- 更新 [CLAUDE.md](CLAUDE.md):
  - "Closure Rules" 段重写：分"Mechanical invariants (code)"、"Audit manifest (code-dispatched, LLM-executed)"、"Semantic field checks (LLM per-check)" 三层
  - 新增一段描述 AuditTask / AuditResult 结构化契约
  - Components 表增加 `build_audit_manifest` 的归属
- 更新 [docs/api.md](docs/api.md):
  - closure-checker 现在按 manifest 审计，tool 调用次数依赖 manifest 大小（NodeBB 典型 ~5-10 项）；`max_budget_usd` 视需要调到 2.5 USD
  - deep-search 新增 reflection 轮次（0.5 USD 预算），每次 deep-search 总成本约 1.5 USD；在文档里写清
- phase17 plan 末尾加指针到 phase18
- 依赖：18.A~F 全部落地

---

**Verification（全流程）**

1. **结构不变量（18.A）**：
   - 单测 I1 双向 / I2 / I3 各 2 个样本
   - phase17 evidence 喂入 I2 必须在 req-014 命中；I1 必须在 req-014/015 双向对得上
2. **审计清单（18.B）**：
   - phase17 evidence 喂入 `build_audit_manifest` → 返回的 manifest 必含 req-008（non-compliant / 或 overlap？若两者皆非则 I2 不触发此项，改成观察 req-014）、req-014
   - 人为让 closure-checker 漏审一项（mock 返回）→ engine 判 EVIDENCE_MISSING
3. **三类语义审计（18.C）**：
   - 人造 evidence：verdict-code 矛盾 1 例；findings 引用不存在代码 1 例；prescriptive 边界失败 1 例 → 每例 AuditResult.per_check 里对应 FAIL
4. **约束传递（18.D）**：
   - 跑 phase17 evidence，生成 PatchPlan.json，每条 FileEditPlan.preserved_findings 非空且含 findings 原文 token
   - patch-generator 拿到的 user prompt dump 里可以看到 evidence.requirements 原文
5. **Deep-search 自省（18.E）**：
   - 构造 forced-hallucination 场景 → reflection 后 findings 反引号片段删除 / 改写
   - 跑真实 NodeBB instance_001，reflection 前后对比 findings token 数
6. **返工分化（18.F）**：三类触发一次，下一轮 deep-search prompt 打印含不同 REWORK INSTRUCTION 段
7. **NodeBB 端到端回归（终极）**：
   - 重跑 issue_001
   - req-014（`db.mget`）verdict 必须改为 TO_BE_MISSING，patch 必须触及 `src/database/{mongo,postgres,redis}/main.js`
   - req-008（`delete.js` expireValidation 缺失）verdict 必须改为 AS_IS_VIOLATED / TO_BE_MISSING，patch 必须触及 `src/user/delete.js`
   - req-013（canSendValidation）findings 必须显式列 ≥ 2 个边界并对 prescriptive 做通过性检验；不能再是单纯 `< max` ↔ `> max` 翻转
   - 3 条 FAIL 测试至少转为 PASS 2 条
   - 总耗时 < 35 min（audit 更重 + deep-search reflection 增加，但不失控）

---

**非目标 / 开放问题（phase 18 明确不做）**

- **Patch self-review 作为独立组件**：按用户判定"证据卡片足够完美则 patch 不应漏需求"，patch 阶段的 verifier 不做。如果 18.A~E 落地后仍有 patch 错误，再考虑"patch-generator 自省轮"（和 18.E 同结构，而非独立组件）。
- **Evidence 更新机制的跨 scope 残留**：scope-based replace 在当前观察下没造成主要失败；若后续发现 rework 后其他 scope 留有过时观测，再开 phase 19 处理。
- **换模型 / 换 relay**：MiniMax-M2.7 的幻觉倾向靠 18.A/B/C/E 从结构上反制；换回官方 Anthropic 时上述 gate 仍有价值但触发率下降，届时不必回滚。
