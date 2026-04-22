## Plan: Phase 17 — Closure-Checker 升级为"审计型"闸门 + 返工路径改造

**背景与根因**

phase16 完成后，在 NodeBB email validation 实例（issue_001）端到端真实评测中观察到三层新问题：

1. **模型在重叠证据上 flip-flop 不收敛**：req-006/007/010/014 同时指向 `src/user/email.js` 54-67 / 131-133 等相同代码段，MiniMax 每次核查给出不同 verdict（req-014 初轮 AS_IS_COMPLIANT、返工轮 AS_IS_VIOLATED）。closure-checker 纯字符串对比触发 Consistency 冲突，re-open → deep-search 重跑 → 得到新 verdict → 新冲突，循环不收敛，21 轮后仍未退出。
2. **先前为了避免 flip-flop 加入的 streak-based 固化机制（"连续 N 轮 verdict 未变则强制 final"）把真冲突也压死了**：一旦模型两次给出同一错误答案，就被锁成 final，即使审计显示引用的文件根本不支持该 verdict 也无法再纠正。
3. **Correct Attribution 全代码化太弱**：[src/orchestrator/guards.py](src/orchestrator/guards.py) 的 `check_correct_attribution` 只检查 `len(evidence_locations) > 0`。模型随手填一个 `src/user/email.js:10-15` 就能过 gate，即便那段代码与 requirement 无关；真正的"引错地方"被延后到 patch 阶段才暴露。
4. **closure-checker 完全不看 repo**：[src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 的 prompt 只让模型读 evidence JSON 判 Consistency，字符串比对法无法区分"verdict 真冲突"与"evidence_location 引错地方导致的伪冲突"，也无法发现"verdict 和 cited 代码实际行为不符"的单 req 错误。
5. **`rounds_remaining=0` 后仍继续 dispatch deep-search**：engine 中 rework 预算判断有 off-by-one 语义错误，rounds_remaining=0 被当作"还剩一轮"，导致超预算时仍重开 5 条 requirement。

设计哲学（承接 phase16）：证据闭环三要素 Sufficiency / Consistency / Correct-Attribution 的校验应**分层**，廉价的机械 gate 负责 100% 可判的格式/非空类检查，高成本的 LLM gate 负责"引用是否真能支撑 verdict"的事实审计。**让 closure-checker 从"字符串裁判"升级为"带 repo 访问的代码审查员"**，并让返工路径有机会真的改变模型答案，而不是用 streak 压制冲突。

**Steps**

### 17.A — 修 `rounds_remaining=0` 超预算仍 dispatch 的 bug（独立可回滚，最先做）

- 定位 [src/orchestrator/engine.py](src/orchestrator/engine.py) rework 预算判断处（日志显示 `rounds_remaining=0` 后仍进入 UNDER_SPECIFIED + dispatch deep-search），将条件从 `if rounds_remaining >= 0` 之类语义改为严格 `> 0` 后才允许 reopen；`== 0` 时 closure-checker 返回 EVIDENCE_MISSING 应直接转入 `CLOSURE_FORCED_FAIL`。
- 依赖：无。
- 验证：构造一个必定反复 EVIDENCE_MISSING 的场景（模型 flip-flop），观察到首次 `rounds_remaining=0` 后不再 dispatch deep-search，pipeline 立即进入 `CLOSURE_FORCED_FAIL`。

### 17.B — 移除 streak-based verdict 固化

- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 定位 "N 轮 verdict 未变则强制 final" 的逻辑（rework 循环中用来短路 flip-flop 的分支）并**整体删除**：这一机制把真冲突也压死了，不应保留。配合 17.D 的差异化 rework prompt，flip-flop 问题应该由"让模型真的换思路"解决，而不是用计数器强压。
- 任何依赖 "verdict streak" 的辅助字段/日志输出一并清理（避免死代码）。
- 依赖：无（但需在 17.D 落地前完成，避免 flip-flop 场景失去任何约束）。
- 验证：grep 全仓 `streak` / `consecutive_same_verdict` / 相关命名应无命中；engine.py 再无 "verdict 未变" 相关的 if 分支。

### 17.C — Closure-checker 升级为带 repo 工具的审计型闸门

- [src/agents/closure_checker_agent.py](src/agents/closure_checker_agent.py) 的 `ClaudeAgentOptions.allowed_tools` 从 `[]` 扩展为 `["Grep", "Read", "Glob", "TodoWrite"]`，权限模式保持 `acceptEdits`（只读，不会改文件）。
- 重写 CLOSURE_CHECKER_SYSTEM_PROMPT：
  - 显式声明职责升级为 "code-reviewer-style audit of EvidenceCards"
  - 输入是 EvidenceCards + repo 访问工具；输出仍为 `ClosureVerdict`（schema 不变）
  - 三段式审计流程（见下）
- **焦点机制（成本控制）**：closure-checker 不做全量审计，按 RequirementItem 分层：
  - `AS_IS_COMPLIANT` 且该 requirement 的 `evidence_locations` **没有**和任何其他 requirement 的引用重叠 → **跳过**（无 defect 声明，无可验证）
  - `AS_IS_COMPLIANT` 但引用位置与其他 requirement 重叠 → **审计**（一边说合规一边说违反，必须定夺）
  - `AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL` → **必审**（defect 声明必须有代码支撑）
  - 重叠判定：任意两条 requirement 的 `evidence_locations` 存在同一 `file:line` 或同一 `file:line-line` 区间的包含关系
- **审计步骤（写进 prompt，closure-checker 在 LLM 层执行）**：
  1. 对每条进入审计集的 RequirementItem，Read 其 `evidence_locations` 中引用的代码段
  2. 判定"该段代码的实际行为"是否支撑该 requirement 的 verdict（Correct-Attribution 审计）
  3. 对所有进入审计集的、引用重叠的 RequirementItem 组，判定两两 verdict 是否与**代码实际行为**一致（Consistency 审计）
- 决策落到 `ClosureVerdict`：
  - `CLOSURE_APPROVED` ← 所有审计集条目的 verdict 与代码一致，且重叠组内无矛盾
  - `EVIDENCE_MISSING` ← 发现某条 req 的 verdict 与代码实际行为不符，或重叠组内矛盾未由代码实际行为支撑
    - `missing` 字段写入"哪条 req 的哪个 evidence_location 与 code 不符 / 哪两条 req 在同一位置矛盾但代码只支持一方"
    - `suggested_tasks` 字段写入建议重跑哪几条 requirement 的 deep-search（id 列表）
- **成本预算**：`max_budget_usd` 从 0.3 USD 提高到 1.5 USD（因为带 Grep/Read/Glob 工具轮，参考 deep-search 的 1.0 但 closure 可能读更多文件，留出余量）；`max_turns` 从 10 提高到 30。
- 依赖：无（但 17.D 依赖本步）。
- 验证：人工构造一份 evidence，包含一条 verdict=AS_IS_COMPLIANT 但 evidence_location 指向与该 requirement 无关的文件的条目，运行 closure-checker 应返回 `EVIDENCE_MISSING` 并在 `missing` 里明确指出问题条目。

### 17.D — 返工路径差异化：verdict 清零 + 审计反馈注入 + 独立 rework prompt

- 当前返工直接重跑同样的 deep-search prompt，导致模型给出相同或轻微摆动的答案。改造如下：
- 在 [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py) 新增 `reset_requirement_for_rework(requirement_id, audit_feedback)` 辅助函数（或复用已有的 `reset_requirement_for_rework`，**先 grep 确认它是否存在且语义是否已符合需求**）：
  - 将目标 RequirementItem 的 `verdict` 置回 `UNCHECKED`
  - 清空其 `evidence_locations` 和 `findings`
  - 把 audit_feedback 文本另存到 RequirementItem 的一个新增字段 `rework_context: str`（默认 ""；下一轮 deep-search 读取并注入 prompt）
- 在 [src/models/evidence.py](src/models/evidence.py) 的 RequirementItem 增加 `rework_context: str = ""` 字段，schema_version 仍保持 "v2"（字段为可选，旧实例默认空串兼容）。
- 在 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 EVIDENCE_MISSING 分支：
  - 不再整体重跑 deep-search 循环；只对 closure-checker 的 `suggested_tasks` 列出的 req id 调 `reset_requirement_for_rework`，注入该 req 特定的审计反馈（从 closure-checker 的 `missing` 字段中解析/提取对应条目）
  - 其余 AS_IS_COMPLIANT 或已 passed 审计的 req 保持不动
- 在 [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py) 的 DEEP_SEARCH_SYSTEM_PROMPT **不改**；但 [src/orchestrator/engine.py](src/orchestrator/engine.py) 的 `_build_deep_search_todo` 在构造 user prompt 时，如果该 req 的 `rework_context` 非空：
  - 在 user prompt 里追加一段显式的 "REWORK INSTRUCTION" 区块，包含审计反馈原文 + "你上一次给出的 verdict 被代码审查驳回，请重新读取相关文件并给出**不同于上次的推理路径**；若重新审视后仍坚持原 verdict，必须在 findings 里引用具体 code line 解释为何审计反馈不成立"
- 依赖：17.C。
- 验证：人工触发一次 closure-checker 返工，观察下一轮 deep-search：
  - 该 req 的 verdict 已在 prompt 喂入前被清零
  - prompt 内包含 REWORK INSTRUCTION 段 + closure-checker 给出的具体审计反馈文本
  - 再下一轮 closure-checker 审计如果仍 EVIDENCE_MISSING，rework_count + 1；达到预算（17.A 规则）后进入 CLOSURE_FORCED_FAIL

### 17.E — Correct-Attribution 代码 gate 保留，不再承担事实核验

- [src/orchestrator/guards.py](src/orchestrator/guards.py) 的 `check_correct_attribution` **保留**但语义显式收窄：只校验"非 AS_IS_COMPLIANT verdict 必须有非空 evidence_locations 且格式合法"。
- 如果 `check_correct_attribution` 发现格式非法（如 evidence_location 不是 `file:line` 或 `file:line-line` 格式），仍按 phase16 规则退回 UNDER_SPECIFIED。
- 事实性审计（引用的位置是否真能支撑 verdict）全部交给 17.C 的 closure-checker。
- 在 guards.py 的 docstring 明确写清楚这一分工，避免后续贡献者又把事实核验塞进代码 gate。
- 依赖：17.C。
- 验证：构造 evidence_locations 含非法格式的 requirement，guards.py 应拦截；构造格式合法但事实错误的 requirement，guards.py 应放行，closure-checker 应拦截。

### 17.F — docs 与可维护性

- 更新 [CLAUDE.md](CLAUDE.md) 的状态机段落：
  - closure-checker 现在持有 Grep/Read/Glob 工具
  - 分层 gate 职责表（机械 vs 审计）
  - 返工路径 verdict 清零 + rework_context 注入的约定
- 更新 [docs/api.md](docs/api.md)：新的 closure-checker 带工具调用这一点在 MiniMax 上的稳定性预期需要注明（工具轮次会被 relay 放大，预算设置 1.5 USD 的原因）
- 更新 [docs/plan/phase16_RequirementItem.md](docs/plan/phase16_RequirementItem.md) 中提到的 closure-checker "判 Consistency only" 说明已过时 → 在 phase16 文档结尾加一行指针到 phase17
- 依赖：17.A~E 全部落地。

**Verification（全流程）**

1. **rounds_remaining 语义**：`rounds_remaining=0` 后 closure-checker 返回 EVIDENCE_MISSING 时，pipeline 直接进入 `CLOSURE_FORCED_FAIL` 而非继续 dispatch deep-search；日志里不再出现 `rounds_remaining=0` 之后的新 iteration。
2. **streak 机制清零**：grep 全仓 `streak` / `consecutive` 应无命中；engine.py 无 verdict 计数类逻辑残留。
3. **审计型 closure-checker 功能性**：构造 4 组人工 evidence 样本，覆盖：
   - 无重叠全 compliant → APPROVED
   - 有重叠 verdict 一致（同位置两个 compliant）→ APPROVED
   - 有重叠 verdict 矛盾（同位置 compliant vs violated）且代码实际支持 violated → EVIDENCE_MISSING，`suggested_tasks` 指向 compliant 的那条
   - 单 req verdict=AS_IS_VIOLATED 但 evidence_location 指向无关文件 → EVIDENCE_MISSING，`missing` 写明"引用与 verdict 不符"
4. **焦点跳过**：构造 15 条全 AS_IS_COMPLIANT 且彼此无位置重叠的 evidence，closure-checker 工具调用数应接近 0（跳过所有），总耗时 < 1 min。
5. **返工差异化 prompt**：人工触发一次返工，下一轮 deep-search 的 user prompt 打印应含 REWORK INSTRUCTION 段 + 前次审计反馈；该 req 在 prompt 喂入前 verdict 已为 UNCHECKED、evidence_locations 已清空。
6. **NodeBB 端到端回归（终极）**：
   - 重跑 issue_001，总耗时 < 30 min（当前 40+ min 未结束）
   - patch 覆盖 requirements 至少 5 条（含 `canSendValidation` 间隔检查、`isValidationPending` 三重条件、`getEmailForValidation` profile-first）
   - action_history 中 closure-checker 调用次数 ≤ 3（包含可能的 1 次 rework）
   - 若发生 flip-flop 而未收敛，应在 rework 预算耗尽后明确进入 CLOSURE_FORCED_FAIL 而不是静默循环
7. **Correct-Attribution 分层**：guards.py 单元覆盖格式非法/空引用两类；closure-checker 覆盖事实错配一类。
