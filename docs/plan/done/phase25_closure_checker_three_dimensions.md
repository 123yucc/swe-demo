# Phase 25: Closure Checker 重构 — 三维度证据闭环

## 背景与动机

### 合格证据闭环的定义

一个合格的证据闭环必须**同时**满足 3 个条件：

1. **Sufficiency（充分性）**：证据是否足够支持一次修复提交。
2. **Consistency（一致性）**：不同证据之间是否相互一致，而不是互相冲突。
3. **Correct attribution（正确归因）**：证据是否真的指向了正确的修复位置和根因，而不是只和 bug 表面现象相关。

当且仅当四类必备 evidence（symptom / constraint / localization / structural）都已达到可提交修复的充分度，彼此之间没有未解决的关键冲突，并且这些 evidence 被一致地归因到同一组根因解释和候选修复位置上时，才认为形成了合格的证据闭环。

### 这次重构要解决的两类失败

- **探索爆炸**：始终无法判断 evidence 是否足够、以及仍需搜集何种信息 → Sufficiency 维度失败。
- **过早提交**：在 evidence 尚未充分、未形成闭环时就 commit → Correct attribution（接地）维度失败。

这两类失败对应的修复对象不同（前者→继续/收敛 deep-search；后者→reset 对应 requirement 重新接地），所以**不应把三个维度塞进同一个 LLM 里**。

### 实现分工（已确认的方向）

- **LLM 质疑者**（重构后的 closure-checker）：负责 **① Sufficiency（语义）+ ② Consistency**。
- **决定性代码门控**：负责 **③ Correct attribution（接地）**——对 evidence 里每一项做 ground 检查。其中：
  - **静态接地**（grep / Read / AST）：本期实现主体。
  - **动态接地**（复现 bug 得到执行路径）：本期仅做设计，留待后续，因为受 hidden test 不可见、复现合成易引入新幻觉等约束。

---

## 现状梳理（重构前）

### 现在的 closure-checker 实际检查什么

当前 closure-checker（`src/agents/closure_checker_agent.py`）**不是**三维度均衡设计。它是 **AuditManifest 驱动的、requirement 级事实接地审计**，只做三种检查：

- `verdict_vs_code` — 引用的 `evidence_location` 代码是否支持 verdict
- `findings_anti_hallucination` — findings 里的反引号代码片段是否真实存在
- `prescriptive_boundary_self_check` — 处方式修复在边界条件下是否成立

也就是说，现在的 closure-checker 做的其实接近 **③接地**，只是用 LLM 来做。而 ①②目前由它**前面的代码门控**承担：

| 维度 | 现状实现 | 位置 |
|---|---|---|
| ① Sufficiency | 形式检查：所有 requirement 非 UNCHECKED | `guards.check_sufficiency` |
| ② Consistency | 弱：anchor 静态门控 + closure-checker 的 overlap 规则 | `consistency_checks.check_consistency_anchors`、`audit.py` Rule 3 |
| ③ Correct attribution | 形式检查（path:LINE 格式）+ LLM 事实审计 | `guards.check_correct_attribution` + closure-checker LLM |

本期把这套分工**重排**：①②交给 LLM 质疑者做语义判断，③下沉为决定性接地门控。

### Schema 变更带来的破损（必须先修）

最近把 `AS_IS_COMPLIANT` 从 `requirements` 移到了 `requirement_status`（`EvidenceCards.requirement_status: list[RequirementStatus]`，见 `src/models/context.py` / `src/models/evidence.py`）。这导致：

- **`build_audit_manifest` 的 Rule 3 失效**（`src/orchestrator/audit.py:135`）：它在 `evidence.requirements` 里找 `verdict == "AS_IS_COMPLIANT"` 来组「overlap 一致性」审计任务，但 compliant 项已不在该列表里 → 这条 cross-requirement consistency 路径已经死掉。
- **`requirement_status` 不进入 `format_for_prompt()`**（`tests/test_requirement_status.py:57` 保证），所以若要让 LLM 质疑者审一致性，必须**显式把 compliant 群重新注入** closure-checker 的输入。
- closure-checker 的 system prompt 仍以「审 requirements」为前提，没有把 compliant 群当作一致性对象的指令。

→ 结论：用户的担心成立。当前 schema 与 checker 已不对应，Rule 3 是死代码，cross-requirement consistency 实质缺失。

---

## ③ Correct Attribution：接地手段的静/动切分

基于对证据卡全字段的审查，**动态执行真正有价值的只有 2 处**，其余静态接地即可，且更安全（不引入执行环境依赖与新幻觉）。

| 卡 / 字段 | 接地手段 | 理由 |
|---|---|---|
| `symptom.observable_failures`（堆栈/异常） | **动态**（复现 bug） | 「症状是否真出现」必须执行才能接地；复现成功即证明是实在 bug 而非表面现象 |
| `localization.call_chain_context` | **动态最强**（实测调用栈） | ③的核心。复现时执行路径若经过 cited location，即证明「在根因路径上」而非表面相关 |
| `localization.exact_code_regions` | **静态**（Read 行范围/内容） | 文件长度、目标行内容核对，静态足够；closure-checker 已在做 |
| `localization.suspect_entities` | **静态**（grep/符号存在性） | 仅需存在性确认 |
| `localization.dataflow_relevant_uses` | **静态**为主 | def-use 适合静态分析，动态过度 |
| `constraint.*`（semantic/behavioral/backward/similar） | **静态**（docstring/assert/参考代码） | 读即可判定，无需执行 |
| `constraint.missing_elements_to_implement` | **静态**（grep 确认**不存在**） | 「不存在」用 grep 证明；动态反而难以证伪 |
| `structural.*`（co_edit / propagation / anchors） | **静态**（已有 anchor / rename-residue 门控） | 已有决定性代码门控 |
| `symptom.repair_targets` / `regression_expectations` | 证据阶段**静态**，验证留给 patch 后 | 属于修复后行为，在此处动态化会与 PatchVerifying 职责重叠 |

### 动态接地的硬约束（为何本期只做设计）

- CLAUDE.md 规定评估者 hidden test（gold test）对 agent 不可见，不能依赖。
- 因此动态复现只能：(a) 从 symptom 自行合成复现脚本，或 (b) 复用 base_commit 已有的 repo 内测试。
- (a) 复现合成本身是新的幻觉来源，且多数 bug 依赖 fixture/服务，难以轻量复现。
- 故动态接地应设计为 **opt-in**：仅当 `observable_failures` 含自洽的复现信息（trace + 触发代码）且构建系统允许轻量执行时才运行，否则回退静态接地。

---

## 目标架构（本期）

```
[决定性代码门控（无 LLM）]
  ① Sufficiency(形式)   : guards.check_sufficiency（保留）
  ③ Correct attribution :
       静态接地（本期实现）: 新模块 grounding.py
            - exact_code_regions / suspect_entities：grep + Read 行范围核对
            - missing_elements_to_implement：grep 确认不存在
            - findings 反引号片段：在 cited region 内 grep 核对（从 LLM 下沉）
            - verdict_vs_code 的「行内容是否匹配」可机械化的部分下沉
       + 既有 anchor / rename-residue 门控
       动态接地（本期仅设计）: 见下「动态接地设计」
       接地失败 → reset_requirement_for_rework(rid)

[LLM 质疑者（= 重构后的 closure-checker）]
  ① Sufficiency(语义)   : 「凭这些证据能不能打一次修复 commit」
  ② Consistency        : 横跨四卡 + active requirements + requirement_status
                          （compliant 群需重新注入），找矛盾
  （prescriptive_boundary_self_check 属语义判断，保留在 LLM 侧）
       EVIDENCE_MISSING → 既有 rework 循环
```

不新增 subagent：③是代码门控，②是 closure-checker 的重新聚焦。这同时回答了「如何在现有 subagents 下完善」。

---

## 文件级落地清单

### 1. 修复 schema 变更的破损

#### 1.1 `src/orchestrator/audit.py`
- [ ] 修复 Rule 3：compliant 项已迁出 `requirements`，改为从 `evidence.requirement_status` 取 compliant 项参与 overlap 检查，或将该职责整体移交 LLM 一致性审计（见 3.x）
- [ ] 复核 `build_audit_manifest` 是否仍假设 compliant 在 `requirements` 中；清除死分支

#### 1.2 `src/models/memory.py`
- [ ] 让 closure-checker 输入能看到 `requirement_status`（一致性审计需要 compliant 群）。注意不要破坏 `test_requirement_status.py:57`「`format_for_prompt` 不含 requirement_status」的契约——通过 closure-checker 专用的输入拼装，而非改 `format_for_prompt`

### 2. ③ 静态接地门控（本期实现主体）

#### 2.1 新增 `src/orchestrator/grounding.py`（纯代码，无 LLM）
- [ ] `ground_exact_code_regions`：对每个 `path:LINE[-LINE]`，确认文件存在、行范围在文件长度内、（可选）该行非空白
- [ ] `ground_suspect_entities`：对带符号的条目用词边界 grep 确认符号存在
- [ ] `ground_missing_elements`：对 `missing_elements_to_implement` 用 grep 确认其确实**不存在**（存在则说明 new_interface 判定矛盾）
- [ ] `ground_findings_snippets`：把 closure-checker 的 `findings_anti_hallucination` 下沉为代码——抽取 findings 反引号片段，在该 requirement 的 cited region 内 grep 核对
- [ ] 统一返回结构（参考 `consistency_checks.py` 的 `AnchorFailure.render()`），每条失败标注 `requirement_id` 以便 reset
- [ ] 复用 `audit.py:_parse_evidence_location` 的解析；避免重复实现

#### 2.2 `src/orchestrator/engine.py`
- [ ] 在 `EVIDENCE_REFINING` 内、closure-checker LLM 之前插入静态接地门控（紧随现有 anchor/structural 门控）
- [ ] 接地失败 → 对每个 `requirement_id` 调 `reset_requirement_for_rework`，feedback 区分接地失败子类（区域不存在 / 符号不存在 / missing_element 实际存在 / findings 片段查无）
- [ ] 预算耗尽时与现有门控一致：放行到 closure-checker，不死循环

### 3. ②①重构后的 closure-checker（LLM 质疑者）

#### 3.1 `src/agents/closure_checker_agent.py`
- [ ] 重写 system prompt：定位为「证据闭环质疑者」，职责 = ① Sufficiency(语义) + ② Consistency
- [ ] **移除** `verdict_vs_code` / `findings_anti_hallucination` 的事实核对（已下沉到 grounding.py）；保留 `prescriptive_boundary_self_check`（语义）
- [ ] Sufficiency(语义)：针对四卡，质问「能否据此打一次修复 commit」——repair_targets 是否落到具体位置、constraint 是否可执行、是否仍有未知信息
- [ ] Consistency：把 active requirements + `requirement_status`（compliant 群）一起喂入，质问 verdict 之间、findings 之间、co-edit 关系之间是否矛盾
- [ ] 输入拼装中显式注入 compliant 群（修 schema 破损）

#### 3.2 `src/models/verdict.py` / `src/models/audit.py`
- [ ] 评估 `ClosureVerdict.audited`（per-task `AuditResult`）是否仍贴合「质疑者」职责；可能从「逐 task 接地结果」改为「逐维度（sufficiency/consistency）质疑结论」
- [ ] 若改结构，同步 `_derive_rework_specs`（`engine.py`）——它现在依赖 `per_check["verdict_vs_code"]` 等键名映射 reset 范围

#### 3.3 `src/orchestrator/audit.py`
- [ ] AuditManifest 与新 checker 职责对齐：去掉已下沉到 grounding 的 check 类型；保留/调整 prescriptive 与一致性所需的 scope 信息

### 4. 动态接地设计（本期仅设计，不实现）

> 目标：单次复现 bug 得到执行路径，为 `symptom.observable_failures` 与 `localization.call_chain_context` 提供接地。

- [ ] 触发条件（opt-in）：`observable_failures` 含自洽 trace + 触发输入；构建系统为可轻量执行类型（复用 `build_verify.detect_build_system`）
- [ ] 复现来源限制：禁止 hidden/gold test；仅允许 (a) 从 symptom 合成最小复现，或 (b) base_commit 内既有 repo 测试
- [ ] 复用 `build_verify.py` 的 subprocess + baseline(`git stash`) 模式，新增「执行并捕获实际堆栈/执行路径」能力
- [ ] 接地判定：实测执行路径是否经过 `cited evidence_locations`；经过→强接地通过，否则记为接地存疑（不强制 fail，避免复现脆弱性误伤）
- [ ] 失败/无法复现时回退静态接地，不阻断流程
- [ ] 明确与 `PatchVerifying` 的职责边界：本接地在**证据阶段**验证根因路径；PatchVerifying 在**补丁后**验证编译/不回归

#### 4.x 贯穿性原则：动态接地必须继承 `unverifiable` 三态语义

`build_verify.py` 在 Go-case 修复中暴露并修正了一个隐患：工具链缺失（`go` 不在 PATH，subprocess rc=127）时，命令"跑不起来"产出空错误列表，被 baseline 相减抵消成"无新增错误"，于是**静默放行**了每一个 Go 补丁。修复方式是在 `BuildCheckResult` 上引入显式的第三态 `unverifiable`，把结果明确切成三类：

- **验证通过**（`ok=True`）：命令真的跑了，且无错误。
- **验证失败**（`ok=False` + `errors`）：命令真的跑了，且解析出结构化错误。
- **无法验证**（`unverifiable=True`）：命令根本没跑起来（工具链缺失 / rc=127），或非零退出却无可归因的错误——**绝不可当作通过**。

动态接地复用的是**完全相同的 subprocess + git stash 模式**，因此会遇到**完全相同的陷阱**：复现脚本因环境/依赖/工具链不可用而跑不起来时，绝不能判成「执行路径未经过 cited location」→ 接地失败，更不能反向误判成「强接地通过」。两种误判都是把"没验证成"伪装成了"验证结论"。

- [ ] 动态接地的执行结果必须采用同一套三态：**复现成功且路径经过 cited location → 强接地通过**；**复现成功但路径未经过 → 接地存疑**（软信号，不强制 fail，见上）；**复现无法执行（环境/工具链/依赖不可用，opt-in 触发条件不满足）→ `unverifiable`**。
- [ ] `unverifiable` 时的行为：**回退静态接地并显式标注**（`grounded_by="dynamic_unverifiable_fallback"`），不阻断流程、不产生任何强/弱接地结论。这把第 164 行「避免复现脆弱性误伤」的保守立场，从一句口头约束**落到代码语义上**——脆弱性导致的"跑不起来"被归入 `unverifiable`，而非被错算成接地信号。
- [ ] 复用 `build_verify.run_build_check` 时直接继承其 `unverifiable` 判定（rc=127 / 无可归因失败），不要在动态接地里重新发明一套"成功/失败"二分。

### 5. 验证与回归

#### 5.1 测试（遵循 CLAUDE.md：禁止 mock，真实双向断言）
- [ ] `grounding.py` 单测：区域越界、符号不存在、missing_element 实际存在、findings 片段查无——各给正反例
- [ ] closure-checker 一致性单测：构造 compliant 与 non-compliant verdict 冲突的 evidence，断言被判 EVIDENCE_MISSING
- [ ] 接地门控 → reset → rework 的端到端链路单测（复用 `tests/test_scoped_evidence_persistence.py` 的 reset 断言模式）
- [ ] schema 破损回归：构造含 `requirement_status` 的 evidence，断言一致性审计能看到 compliant 群

#### 5.2 6 个 case 回归
- [ ] rerun，重点看：探索爆炸是否因 Sufficiency 语义质疑而收敛；过早提交是否因静态接地门控而被拦下
- [ ] 确认新门控不误伤已通过 case

---

## 缺口补全设计（本轮确认）

> 第 1–5 节把「①②交 LLM、③下沉代码门控」的方向理清了，但落地清单留了三个会改变实现走向的缺口。本节把三者补全；三个分叉点已逐一确认（见每节「决策」）。

### 缺口 A — call_chain / dataflow / symptom 的静态接地深度

**问题**：第 63–77 行的接地表把 `localization.call_chain_context`、`localization.dataflow_relevant_uses`、`symptom.observable_failures` 推给了「动态接地」，而动态接地本期只设计不实现（第 4 节）。结果这三个字段本期**没有任何机械接地**——其中 `call_chain_context` 还被文档自称为「③接地的核心」。grep 只能证明「符号存在」，证明不了「调用链确实经过此处」或「def-use 关系成立」，所以单靠 `grounding.py` 的词边界 grep 填不上这个洞。

**决策：本期实现跨语言 AST 静态接地**（不再等动态接地）。理由：这三个字段的接地本质是**结构关系核对**（caller→callee 边是否存在、def 与 use 是否在同一作用域、symptom 提到的符号/异常类型是否在代码里有定义点），AST 能在不执行代码的前提下给出比 grep 强得多的接地，且无执行环境依赖、不引入复现幻觉。

#### A.1 新增 `src/orchestrator/ast_grounding.py`（纯代码，无 LLM）

跨语言解析器抽象 + 按语言分发：

- [ ] `class SymbolIndex`：对一个文件解析出 `defs`（函数/方法/类定义点，带 `name`/`lineno`/`scope`）、`calls`（调用点，带 `callee_name`/`lineno`/`enclosing_def`）、`names`（标识符读写点，区分 load/store，用于 def-use）。
- [ ] `def build_symbol_index(path: str, source: str) -> SymbolIndex | None`：按扩展名分发解析后端；解析失败或语言不支持返回 `None`（调用方回退到 grep 接地，**不报 fail**）。
- [ ] **后端**：
  - Python（`.py`）：标准库 `ast`，零依赖，作为基线后端先落地。
  - 跨语言（`.go` / `.js` / `.ts` / `.java` 等）：`tree-sitter` + 对应 grammar。**新增依赖**，写入 `requirements.txt`；grammar 加载失败时该语言降级为 grep（与解析失败同路径）。
- [ ] 统一查询接口（语言无关）：
  - `has_call_edge(index, caller_name, callee_name) -> bool`：caller 的 def 体内是否存在对 callee 的调用点。用于 `call_chain_context` 形如 `A -> B` 的接地。
  - `resolves_def_use(index, var_name, def_line, use_line) -> bool`：`var_name` 在 `def_line` 有 store、在 `use_line` 有 load，且二者作用域可达。用于 `dataflow_relevant_uses`。
  - `has_symbol_def(index, name) -> bool` / `has_exception_class(index, name) -> bool`：用于 `symptom.observable_failures` 里提到的符号/异常类型在代码中确有定义点。

#### A.2 在 `grounding.py` 中接线（接地手段升级，非新门控）

- [ ] `ground_call_chain`：解析 `call_chain_context` 条目里的 `Caller -> Callee`（已有的 `->` / `→` 箭头约定），用 `has_call_edge` 核对边存在；AST 不可用→回退 grep「两个符号都在 cited 文件出现」并降级为 **soft pass**（记 `grounded_by="grep_fallback"`，不 fail）。
- [ ] `ground_dataflow_uses`：对带 `path:def_line ... path:use_line` 结构的条目用 `resolves_def_use` 核对；无结构化行号→回退 grep 存在性 soft pass。
- [ ] `ground_symptom_symbols`：抽取 `observable_failures` 里的反引号符号 / `XxxError`/`XxxException` 形 token，用 `has_symbol_def` / `has_exception_class` 核对其在仓库有定义点；查无→fail（symptom 引用了不存在的符号是真幻觉）。
- [ ] **硬约束**：AST 接地**只把「明确证伪」当 fail**（边不存在、def-use 跨作用域不可达、符号无定义点）；解析不了 / 语言不支持 / 信息不足以判定，一律 **soft pass + 标注**，避免 AST 脆弱性误伤。这条与第 4 节动态接地「不强制 fail」的保守立场一致。

#### A.3 与动态接地的边界（不变）

AST 接地补的是**结构**（边/作用域/定义点是否存在），动态接地（后续期）补的是**执行**（运行时是否真的走到 cited location）。A 落地后，第 4 节动态接地从「核心接地手段」降级为「对 call_chain 的强化确认」，本期仍只设计。

---

### 缺口 B — 全局卡片字段的返工锚定

**问题**：接地与 reset 都以 `RequirementItem.evidence_locations` 为锚，但 `symptom.*`、`constraint.semantic_boundaries` 等**全局卡片字段没有 requirement_id**。接地失败时无法像 RequirementItem 那样定向 `reset_requirement_for_rework(rid)`。

**决策：建「卡片字段条目 → requirement」反向索引，按它把全局字段失败归属到具体 req 上 reset；匹配不到则标 `<global>` 回退到 UNDER_SPECIFIED**（与现有 `check_consistency_anchors` 的 `<global>` 兜底完全一致，复用既有约定，不发明新机制）。

#### B.1 在 `grounding.py` 新增 `attribute_field_failure_to_req`

- [ ] 输入：失败条目文本（如某条 `observable_failures` 或 `semantic_boundaries`）、当前 `EvidenceCards`。
- [ ] 匹配优先级（先命中先返回）：
  1. **路径重叠**：条目里若含 `path:line` 形位置，取 path，匹配 `evidence_locations` 路径相同的 req（复用 `consistency_checks.py` 已有的 `path_to_reqs` 索引构建法，第 211–217 行）。
  2. **token 重叠**：复用 `guards._keyword_overlap`（min_shared=2），把条目文本与每个 req 的 `text + findings` 比对，取重叠最高的 req。
  3. **scoped_evidence 反查**：该字段值若与某 req 的 `scoped_evidence.localization/constraint/structural` 切片内容一致，归该 req（`scoped_evidence` 本就是 per-req 来源，是最强信号——见 `evidence.py:189` ScopedEvidence 注释）。
- [ ] 全不命中 → 返回 `"<global>"`。
- [ ] 返回 `(requirement_id, matched_by)`，`matched_by ∈ {path, token, scoped, global}` 写入失败记录便于调试。

#### B.2 engine 接线

- [ ] 静态接地门控收集失败后，对每条失败调 `attribute_field_failure_to_req`：
  - 命中具体 rid → 并入该 rid 的 reset 集合，feedback 注明「全局字段 X 接地失败，疑似源自本需求的调查」。
  - `<global>` → 走现有 `check_consistency_anchors` 的 `<global>` 同款处理：不针对单 req reset，而是整体回退 `UNDER_SPECIFIED` 并把失败列表写入下一轮 deep-search 的全局上下文。
- [ ] 预算耗尽时与现有门控一致：放行到 closure-checker，不死循环。

---

### 缺口 C — 维度化后的结构化返工映射（维度 → operator → 字段）

**问题**：checker 从「逐 task PASS/FAIL」改为「逐维度质疑结论」后，`_derive_rework_specs`（`engine.py:92`）依赖的 `per_check["verdict_vs_code"]` 键名映射全部失效。文档第 20 行指出两类失败「修复对象不同」，但没编码。同时第 99–101 行已明确**反对让 LLM 自己报字段名**（会报非法字段）。

**决策：逻辑上分两个 operator（`deepen` / `reconcile`），物理上共用同一条 `reset_requirement_for_rework → deep-search` 循环与同一个 `rework_rounds_max` 预算。** 不拆成两条独立控制流——独立的 Sufficiency 循环需要自己的预算守卫和 TODO 再派发，等于重造一遍已验证的 rework 预算机制，并新增「Sufficiency 永远说不够」的无限循环风险。靠「选哪些 req + 用哪个反馈模板」区分两个 operator，既编码了「修复对象不同」，又不新增控制流分支。

#### C.1 `ClosureVerdict` 结构改造（`src/models/verdict.py`）

- [ ] `audited: list[AuditResult]` 保留 `prescriptive_boundary_self_check` 一项（仍是 LLM 语义判断），但**移除** `verdict_vs_code` / `findings_anti_hallucination`（已下沉到 grounding，第 145 行）。
- [ ] 新增 `dimension_findings: list[DimensionFinding]`，每项是 LLM 对一个维度的质疑结论。`DimensionFinding`（新增于 `src/models/verdict.py` 或 `audit.py`）：
  - `dimension: Literal["sufficiency", "consistency"]`
  - `status: Literal["PASS", "FAIL"]`
  - `requirement_ids: list[str]` — 与该质疑相关的 req（consistency 可多个，sufficiency 通常单个或空）
  - `conflicting_field: str | None` — **从固定枚举里选**，不是自由文本（防非法字段名）：`{"verdict", "findings", "evidence_locations", "repair_targets", "missing_elements", "<cross-req>"}`
  - `explanation: str` — 一句话理由，写入 rework_context
- [ ] `verdict` 仍为 `CLOSURE_APPROVED` / `EVIDENCE_MISSING`：任一 `dimension_findings.status == FAIL` 或任一 `audited` 有 FAIL → `EVIDENCE_MISSING`。

#### C.2 重写 `_derive_rework_specs`（`engine.py:92`）—— 维度 → operator → 字段映射表

确定性映射（不让 LLM 报字段，LLM 只报维度 + 枚举字段 + 相关 req）：

| dimension | status | operator | reset 范围 | feedback 模板 |
|---|---|---|---|---|
| sufficiency | FAIL | **`deepen`** | 该 req 全量 reset（`None`）| 「证据不足以支撑一次修复 commit：<explanation>。深化定位/补全 <conflicting_field>」 |
| consistency | FAIL | **`reconcile`** | `conflicting_field=="<cross-req>"` → 涉及的所有 `requirement_ids` 全量 reset；否则 reset 指定字段 | 「与 req-X 的 <field> 矛盾：<explanation>。本轮必须给出不同于前一 verdict 的推理路径」 |
| prescriptive (audited) | FAIL | `reconcile` | `{"findings"}`（沿用现状）| 现有处方式边界反馈 |

- [ ] 两个 operator 都最终调用 `reset_requirement_for_rework(rid, audit_feedback, fields_to_reset)`——**物理路径不变**，只是 `fields_to_reset` 和 `audit_feedback` 由上表决定。
- [ ] operator 名（`deepen`/`reconcile`）写入 `memory.record_action` 的 outcome，便于事后区分「探索爆炸被 Sufficiency 收敛」还是「过早提交被 Consistency 拦下」（呼应 5.2 回归目标）。
- [ ] 共用 `rework_rounds_used / rework_rounds_max`；耗尽 → `CLOSURE_FORCED_FAIL`（现状不变）。
- [ ] 全局字段失败（缺口 B 的 `<global>`）不进本映射表，走 B.2 的整体回退路径。

#### C.3 closure-checker prompt 对齐（`src/agents/closure_checker_agent.py`）

- [ ] prompt 产出 `dimension_findings` 时，`conflicting_field` 限定在上述枚举内（prompt 里显式列出可选值）；`requirement_ids` 必须引用输入中实际存在的 req id。
- [ ] consistency 维度的输入必须含 compliant 群（缺口已在 1.2 / 3.1 列出，此处复用）。

#### C.4 测试（遵循 CLAUDE.md 禁 mock，真实双向断言）

- [ ] 构造 sufficiency-FAIL 的 verdict（repair_targets 未落位）→ 断言 operator=deepen、对应 req 被全量 reset、feedback 含 deepen 模板。
- [ ] 构造 consistency-FAIL 跨两 req 的 verdict → 断言 operator=reconcile、两 req 均被 reset。
- [ ] 构造 `conflicting_field` 取枚举外值的 verdict → 断言被拒/回退，不写入非法字段。

---

## 建议实施顺序（MVP）

> 实施状态（本轮完成）：第 1–7 步已落地，第 8 步（动态接地）按设计留待后续单独成期。

1. [x] **修 schema 破损**（`audit.py` Rule 3 删除、compliant 群经 `_format_compliant_group` 注入 closure-checker）
2. [x] **静态接地 `grounding.py`** + engine 接线（`run_static_grounding` 在 closure-checker LLM 前，紧随 anchor 门控）
3. [x] **AST 接地 `ast_grounding.py`**（缺口 A）——Python `ast` 基线后端已落地，tree-sitter 跨语言为可选依赖（不可用即降级 grep soft-pass）
4. [x] **全局字段返工锚定**（缺口 B）——`attribute_field_failure_to_req`（path/token/scoped 三级）+ engine `<global>` 非阻断回退
5. [x] **重写 closure-checker** 为 ①②质疑者，移除已下沉的 check，注入 compliant 群
6. [x] **维度化返工映射**（缺口 C）——`ClosureVerdict.dimension_findings` + `_derive_rework_specs`（deepen/reconcile，`RworkSpec`）+ AuditManifest 对齐
7. [x] 测试（`tests/test_grounding.py`、`tests/test_closure_checker_phase25.py`、`tests/test_scoped_evidence_persistence.py` 维度化重写）——全套 55 passed
8. [ ] **动态接地**：按本文档第 4 节，后续单独成期实现（A 落地后降级为对 call_chain 的强化确认）

---

## 决策记录

- **不新增 subagent**：③接地是决定性处理（静态=纯代码门控，动态=subprocess 门控），放进 LLM 会重新引入想消除的幻觉；与既有「代码门控 → 最后 LLM」结构一致（`build_verify.py` 即「无 LLM subprocess 门控」先例）。
- **接地门控采用「通过 / 失败 / 无法验证」三态语义**：上述 `build_verify.py` 先例现已明确区分这三态——`ok=True`（跑了且通过）/ `ok=False`+`errors`（跑了且失败）/ `unverifiable=True`（工具链缺失或命令跑不起来，**绝不放行**）。该三态是从一个真实事故里固化下来的：缺 `go` 工具链时 rc=127 产出空错误，被 baseline 相减抵消成「无新增错误」，静默放行了每个 Go 补丁。Phase25 的接地门控（静态 `grounding.py` / 动态复现）都应继承同一套三态语义——把「没验证成」与「验证通过」严格分开，避免重蹈「缺工具链时静默放行」的覆辙。静态侧体现为 soft-pass + `grounded_by` 标注，动态侧体现为 `unverifiable` 回退静态接地（见第 4.x 节）。
- **动态接地仅 opt-in**：受 hidden test 不可见 + 复现合成易幻觉约束，全量动态执行不可取；多数字段静态接地更安全。
- **closure-checker 聚焦 ①②**：事实接地下沉后，LLM 专注语义层的充分性与一致性质疑，失败信号与前置门控失败信号分离，便于定位「前面没拦住」还是「补丁真写坏了」。
- **本期实现 AST 接地（缺口 A）**：`call_chain_context` / `dataflow_relevant_uses` / `symptom` 的接地本质是结构关系核对（边/作用域/定义点存在性），AST 能在不执行代码下给出远强于 grep 的接地，且无执行依赖、不引入复现幻觉。AST 只把「明确证伪」当 fail，解析不了/信息不足一律 soft-pass，避免脆弱性误伤。tree-sitter 是新增依赖，grammar 不可用即降级 grep。
- **全局字段返工复用 `<global>` 兜底（缺口 B）**：symptom/constraint 等无 requirement_id 的字段，先经反向索引（path / token / scoped_evidence 三级）归属到具体 req 定向 reset，匹配不到才退回 UNDER_SPECIFIED，复用 `check_consistency_anchors` 既有约定，不发明新机制。
- **两个 operator 共用一条 reset 循环（缺口 C）**：`deepen`（Sufficiency 失败→深化调查）与 `reconcile`（Consistency 失败→重新接地冲突 req）逻辑上区分修复对象，物理上共用 `reset_requirement_for_rework → deep-search` 循环与同一个 `rework_rounds_max` 预算。不拆独立控制流，避免重造预算守卫并消除「Sufficiency 永远说不够」的无限循环风险。LLM 只报维度 + 枚举字段 + 相关 req，字段名映射由代码确定，杜绝非法字段。
