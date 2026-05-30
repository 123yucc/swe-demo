# Phase 24: 短期记忆 + 长期记忆

> **DEPRECATED — 本方案已被废弃。**
>
> 长期记忆改为直接接入 [QuantaAlpha/MemGovern](https://github.com/QuantaAlpha/MemGovern)
> 的 13w 条经验数据库，不再实施本文档中的 `RepairTask` / `CodebaseScenario` /
> reflection / distill / promote / SQLite / `case_outcomes` / `eval_result` 回灌等
> 子模块。
>
> 当前实际方案见 [phase24-long-term-memory-memgovern.md](./phase24-long-term-memory-memgovern.md)。
>
> 本文档保留作为历史背景，**不应作为实施依据**。

---

## 目标

把当前的 `SharedWorkingMemory` 从"共享上下文容器"升级为"当前 case 的动态问题模型"，
同时建立一套跨 case 的长期经验库。长期经验库要能帮助后续 case：

- 更早识别问题类型
- 更快定位证据
- 避开已知踩过的坑

核心原则：**长期记忆只提供参考经验，不替代当前 case 的代码证据**。

本阶段对 phase23 零依赖,可以独立实施。

---

## 1. 现状与缺口

### 1.1 短期记忆现状

`src/models/memory.py::SharedWorkingMemory` 已有：`issue_context`、`evidence_cards`、
`patch_plan`、`retrieved_code`、`action_history`。作为共享上下文够用,但缺一项：
case 画像（repo、语言、failing_test_signals 等），导致无法用作长期记忆的查询入口。

> 注：早先方案讨论过同时引入 `failed_attempts` 字段,后已砍掉。patch-generator
> 的失败原因不需要持久化到 working memory，由 SDK 会话日志自然承接。

### 1.2 长期记忆现状

完全没有。

### 1.3 `patch_outcome.json` 现状

当前字段：`issue_id`、`closure_checker_approved`、`patch_outcome`。
**只反映 closure-checker 和 patch-generator 的结果，不包含 eval 测试结果。**

Eval 的真实结果存放在：
- `workdir/<batch_name>/eval_results.json` — 每个 instance 是否 pass
- `workdir/<batch_name>/<instance_dir>/_output.json` — 每个测试的详细结果

这两个文件是评测批次跑完后产生的独立文件，orchestrator 不知道它们的存在。
要让长期记忆依据真实的测试结果来判断"这个 case 是不是真的修对了"，
必须把 eval 结果回灌进 `patch_outcome.json`。

---

## 2. 短期记忆升级

对 `SharedWorkingMemory` 只加一个字段：`case_profile`。

### 2.1 `case_profile`

当前 case 的静态画像，初始化一次后只读，主要用作长期记忆的查询入口。

```python
class CaseProfile(BaseModel):
    issue_id: str
    repo_name: str                     # e.g. "NodeBB/NodeBB"
    subsystem_hint: str = ""           # 由 suspect_entities 路径前缀派生
    language: str = ""                 # 由 repo 文件扩展名派生
    framework_hints: list[str] = []    # 由 package.json / requirements.txt 派生
    failing_test_signals: list[str] = []
        # 从 symptom.observable_failures 抽取的关键词
        # 例如 ["empty", "falsy", "not_found", "forbidden", "default"]
        # 这是 task 检索的唯一钥匙
```

不再设 `archetype_candidates`：bug 类型的离散标签系统已整体放弃,见 §3.2。

### 2.2 不再引入 `failed_attempts`

patch-generator 失败原因不写入 working memory。SDK 已把每次 query 的完整对话
持久化到 `~/.claude/projects/<encoded-cwd>/*.jsonl`，需要时从那里读，不再
在 working memory 重复一份。下一轮 planner 需要规避的策略由 orchestrator
直接以 prompt 字段形式传递，不持久化。

---

## 3. 长期记忆设计

### 3.1 两张表（一张知识表 + 一张索引表）

长期记忆只分两类。**所有可执行知识集中在 `RepairTask`，`CodebaseScenario`
退化为纯索引表**，避免两张表持有同主题的自由文本知识造成冗余和幻觉。

#### A. `CodebaseScenario` — repo/子系统索引节点

只回答"这个 repo/子系统下，历史上沉淀过哪些 task 经验"，本身不持有自由文本
知识。所有原本计划放在 scenario 的可执行知识（`architectural_notes`、
`recurring_contract_locations`、`known_pitfalls`）都改放进 task。

```python
class CodebaseScenario(BaseModel):
    scenario_id: str                   # = hash(repo_name + subsystem)
                                       # 主键，同时承担去重职责
    repo_name: str
    subsystem: str = ""
    language: str = ""
    framework_hints: list[str] = []
    related_task_ids: list[str] = []   # 这个 repo/子系统下命中过哪些 task
    source_case_ids: list[str] = []
    confidence: Literal["provisional", "confirmed", "suspect"]
    success_hits: int = 0
    failure_hits: int = 0
    last_used_at: datetime | None = None
    created_at: datetime
```

scenario 的 `related_task_ids` 由 promote 阶段在 task 晋升入主表时自动维护，
不靠 Reflection Agent 单独产出 scenario 候选。

#### B. `RepairTask` — 修复任务级知识表

回答"这类 bug 通常长什么样、怎么修、哪里容易翻车"。**所有跨 case 经验都集中在这里**。

```python
class RepairTask(BaseModel):
    task_id: str                       # = hash(sorted(task_signature)[:5])
                                       # 主键，同时承担去重职责
    task_signature: list[str]          # 检索钥匙，3~8 条关键词
                                       # 是检索时唯一参与匹配的字段

    completeness_checklist: list[ChecklistItem]
        # 合并了原 evidence_must_cover + typical_coedit_set
        # 每条带 phase 标签区分注入阶段
    success_recipe: list[SuccessRecipeItem]         # 从 passed case 蒸馏
    failure_antipatterns: list[FailureAntipatternItem]  # 从 passed/failed case 蒸馏
                                                         # 含原 known_pitfalls 内容

    source_case_ids: list[str]
    confidence: Literal["provisional", "confirmed", "suspect"]
    success_hits: int = 0
    failure_hits: int = 0
    last_used_at: datetime | None = None
    created_at: datetime
```

字段精简点回顾：
- 删除 `archetype`：bug 类型字符串系统整体放弃
- 删除 `signature_hash` 和外部 `dedup_key`：`task_id` 直接由签名 hash 担任
- 删除 `evidence_must_cover` / `typical_coedit_set`：合并为 `completeness_checklist`
- 删除 scenario 侧的 `architectural_notes`：无明确下游用途，且与 README/package 信息重复


### 3.3 存储：SQLite

本地 SQLite（Python 自带，零外部依赖），WAL 模式支持并发。

```
long_term_memory/memory.db
├── scenarios      表：索引主表，retrieve 时只查这里
├── tasks          表：知识主表，retrieve 时只查这里
├── candidates     表：缓冲区，新产出的经验先进这里，不被 retrieve
├── usage_log      表：每次 retrieve 命中记录 (memory_id, case_id, hit_time)
└── case_outcomes  表：case_id、final_state、eval_status、eval_failing_tests
```

几百 case 的规模够用。不引入 PostgreSQL / 向量库 / embedding。

---

## 4. Eval 结果回灌

### 4.1 `patch_outcome.json` 扩字段

```json
{
  "issue_id": "...",
  "closure_checker_approved": true,
  "patch_outcome": "PATCH_SUCCESS",
  "eval_result": {
    "status": "passed" | "failed" | "unknown",
    "passed_tests": 42,
    "failed_tests": 1,
    "failing_test_names": ["test/.../test_open_url"],
    "source_path": "workdir/batch_001_006_eval_result_rerun/eval_results.json",
    "backfilled_at": "2026-05-12T..."
  }
}
```

orchestrator 首次写入时 `eval_result.status = "unknown"`。

### 4.2 回灌脚本

新增 `src/memory/backfill_eval.py`：

```
python -m src.memory.backfill_eval --batch workdir/batch_xxx/
python -m src.memory.backfill_eval --all
```

行为：
- 读 `<batch>/eval_results.json` 得到 instance_id → pass 的映射
- 读 `<batch>/<instance_dir>/_output.json` 得到每个测试的详细结果
- 按 `instance_id` 反查 `workdir/<issue_name>/outputs/patch_outcome.json`，
  回写 `eval_result` 字段
- 幂等，可重跑

`issue_name` 与 `instance_id` 的映射从已有的 `instance_metadata.json` 读。

---

## 5. Reflection Agent

### 5.1 职责与模型

一个新 agent，负责从单个 case 蒸馏出"可迁移的经验候选"。
模型：**Claude Sonnet 4.6**（下游有结构验证和跨 case 阈值兜底，
不需要 Opus 的成本）。

文件：`src/agents/reflection_agent.py`。

**Reflection 只产出 task 候选**。scenario 行不由 LLM 产出,而是 promote
阶段在 task 候选晋升入主表时,根据该 task 关联的 case 自动派生/更新对应
scenario 的索引信息。

### 5.2 触发时机

`patch_outcome.json` 的 `eval_result.status` 被回灌之后，且仅当 status ∈
{`passed`, `failed`}。`unknown` 永不触发。

执行入口：

```
python -m src.memory.reflect --issue <issue_name>
python -m src.memory.reflect --batch <batch_dir>
```

### 5.3 输入

严格限定输入范围，避免信息爆炸带来的联想：

- `evidence_cards`（最终态）
- `patch_plan`
- `patch.diff` 全文
- `patch_outcome.eval_result`
- `case_profile`

**不注入**已有长期记忆，避免 reflection 自我强化 / 自我肯定。
**不再注入** `failed_attempts`（该字段已删除）。

### 5.4 输出

SDK 结构化输出，不允许自由文本。三个知识字段以**槽位互斥的提问式 schema**
表达，从形式上让它们互不可混（详见 §6.4 形态约束）：

```python
class ChecklistItem(BaseModel):
    """完整性清单条目：合并了原 evidence_must_cover 与 typical_coedit_set。"""
    text: str                          # 名词短语形式，禁含动词
    phase: Literal["evidence", "patch"]
        # evidence: deep-search 阶段需要确认的事实维度
        # patch:    patch-planning 阶段需要一起改动的位置/层次关系
    sources: list[Source]              # 必须非空

class SuccessRecipeItem(BaseModel):
    """修复动作。"""
    text: str
        # 陈述句，描述核心修复动作
        # 禁含位置词（"同时" / "记得" / "注意" / "also" / "remember"）
        # 禁含文件路径或层次词
    sources: list[Source]              # 必须非空

class FailureAntipatternItem(BaseModel):
    """失败反模式（含原 known_pitfalls 语义）。"""
    text: str                          # 自由文本,但仍受文本黑名单约束
    sources: list[Source]              # 必须非空

class Source(BaseModel):
    """经验文本的来源位置。"""
    kind: Literal["diff", "requirement", "test"]
    ref: str
    # diff:<file>:<hunk_idx>:<line_in_hunk>
    # requirement:<req_id>
    # test:<index_in_failing_test_names>

class TaskCandidate(BaseModel):
    task_signature: list[str]          # 3~8 条关键词
    completeness_checklist: list[ChecklistItem]
    success_recipe: list[SuccessRecipeItem]
    failure_antipatterns: list[FailureAntipatternItem]

class ReflectionOutput(BaseModel):
    case_id: str
    eval_status: Literal["passed", "failed"]
    task_candidates: list[TaskCandidate]    # 0..2
    rationale: str                          # 100~400 字
```

### 5.5 输入场景 vs 输出字段

| eval_status | 输出字段 |
|---|---|
| `passed` | `completeness_checklist`、`success_recipe` |
| `failed` | `failure_antipatterns`、可选的 `completeness_checklist`(phase=patch) |

一个 case 最多产出 **2 条 task 候选**（不再产出 scenario 候选）。
超出上限直接拒绝整份输出，由 distill 阶段重试一次。

### 5.6 提问式 prompt 形态约束

为避免 `completeness_checklist` / `success_recipe` / `failure_antipatterns`
三类知识在 LLM 自由文本里互窜，prompt 把它们包装成**三个语法形态不同的问题**：

- Q1 → `completeness_checklist[phase=evidence]`：
  "再遇到一个同类 bug 时，要宣告'我已经看懂了'必须确认哪些事实？
  每条以名词短语形式回答，**不含动词**，≤8 词。"
- Q2 → `completeness_checklist[phase=patch]`：
  "本次 patch 改动的层次/文件类型有哪些？跨层次搭配关系是什么？
  每条以 `层次A ↔ 层次B` 格式回答，**必须含 ↔**。"
- Q3 → `success_recipe`：
  "用一句陈述句描述本次修复的核心动作，**不含位置词**
  （'同时'/'记得'/'注意'/'also'/'remember'），**不提文件路径或层次词**。"

形态约束由 Pydantic validator 在结构层执行（见 §6.4）。

---

## 6. 防幻觉

分四层。前三层为继承的设计，第四层为新增的形态约束。

### 6.1 出处必填

每条 `ChecklistItem` / `SuccessRecipeItem` / `FailureAntipatternItem` 都带
`sources` 列表，必须非空，每条 source 指向：

- patch.diff 里的具体 hunk 行
- evidence_cards 的某个 requirement id
- failing_test_names 的某个下标

Pydantic validator 拒绝 `sources == []` 的条目。prompt 里要求每条经验附出处。

### 6.2 出处真实性复核

distill 阶段不信任 LLM 自报，做一次**机械验证**：

- 打开 patch.diff / evidence 原文，确认每个 source 引用真实存在
- 比对经验 `text` 与 source 附近文本，要求至少 2 个非停用词 token 重叠
- 任一 source 对不上 → 丢弃**这一条** item（不是整份输出），粒度可控

### 6.3 文本黑名单

validator 层拒绝：

- 含具体行号（正则 `:\d+$` 等）
- 含超过 60 字的 diff 原文片段
- 是空话短语：`"fix the bug"`、`"correctly handle"`、`"properly implement"` 等

命中任一条 → 拒绝该条目。

### 6.4 形态正交（新增）

承接 §5.6 的提问式 prompt，由 Pydantic validator 在结构层做形态校验，
让三类知识无法互窜：

- `ChecklistItem.text` (`phase=evidence`)：禁含动词（用轻量词性启发式
  或动词词典),命中即拒绝该条目
- `ChecklistItem.text` (`phase=patch`)：必须含关系符 `↔`,缺失即拒绝
- `SuccessRecipeItem.text`：黑名单匹配位置词
  `{"同时", "记得", "注意", "also", "remember", "don't forget"}`,
  以及匹配文件路径正则 `/[\w/]+\.\w+/` 与层次词
  `{"controller", "handler", "middleware", "model", "view", ...}`
  ；命中任一项即拒绝

形态校验失败 → 该条目丢弃（与 §6.2 一致的丢弃粒度），不影响整份输出
其他合规条目落库。

---

## 7. 防过拟合

一个 case 自己的教训不能直接进库。

### 7.1 candidates 缓冲区

Reflection 的产出先写进 `candidates` 表，不进主表。
`candidates` 不参与 retrieve。

### 7.2 单 case 写入上限

每个 case 最多贡献 **2 条 task 候选**（scenario 候选不再由 LLM 产出，
不存在写入上限的问题）。在 Pydantic 层强制（5.4）。

### 7.3 跨 case 晋升阈值 N = 2

从 `candidates` 晋升到主表的条件：

- 同一个 `task_id`（即相同的 signature hash）被 **≥ 2 个不同 case** 独立贡献过
- 晋升时合并这些 candidate 的内容，进入主表，`confidence = "provisional"`
- 单个 case 独享的候选永远留在 candidates 表，不会被 retrieve
- task 晋升入主表时,promote 阶段同步在 `scenarios` 表派生/更新对应
  scenario 的 `related_task_ids`、`source_case_ids` 等索引字段

晋升不靠手工，由 `src/memory/promote.py` 批量自动执行。

### 7.4 使用后回看

- retrieve 命中某条主表记忆 → 记一条 `usage_log(memory_id, case_id)`
- 该 case 的 eval 回灌后：
  - `passed` → 命中过的记忆 `success_hits++`
  - `failed` → 命中过的记忆 `failure_hits++`
  - `unknown` → 不计
- 自动状态变化：
  - `provisional → confirmed`：`success_hits ≥ 3` 且成功率 ≥ 70%
  - `任意 → suspect`：`failure_hits ≥ 3` 且成功率 < 30%
  - `suspect` 状态的记忆 retrieve 时直接跳过

### 7.5 TTL

`provisional` 状态的记忆超过 30 个 case 没被 retrieve 命中过 → 自动清理。

---

## 8. 去重

主键即去重键，不再有独立的 `dedup_key` / `signature_hash` 概念。

### 8.1 写入 candidates 时

- task 候选：以 `task_id = hash(sorted(task_signature)[:5])` 为键。
  键已存在 → 合并：`source_case_ids` 并集、经验条目取并集但
  **文本 Jaccard 相似度 > 0.7 视为重复，只保留一条**。
- scenario 行：以 `scenario_id = hash(repo_name + subsystem)` 为键。
  scenario 不接收 LLM 产出，仅在 task 晋升时派生/更新；同 scenario_id
  即累积 `related_task_ids`、`source_case_ids` 的并集。

### 8.2 candidates → 主表时

同样按 `task_id` + Jaccard 比对。命中则合并进已有主表条目，不新建。

不用 embedding，token 层面的 Jaccard 足够且零成本。

---

## 9. 检索

入口：`src/memory/retrieve.py`。

### 9.1 查询钥匙

从 `case_profile` 派生两组钥匙：

- scenario 钥匙：`(repo_name, subsystem_hint, language)` —— 用于查 scenario
  索引表，但 scenario 本身不持有可执行知识，仅作为反查 task 的入口
- task 钥匙：`failing_test_signals` —— 与每条主表 task 的 `task_signature`
  做 Jaccard 相似度，取 top-3

合计最多 3 条 task 进 prompt（scenario 命中后通过 `related_task_ids`
反查的 task 也并入此 top-3，不重复计数）。

### 9.2 何时检索、检索什么

| 阶段 | 检索 task |
|---|---|
| `UNDER_SPECIFIED` 起点 | `completeness_checklist[phase=evidence]` |
| deep-search 轮次开始 | `completeness_checklist[phase=evidence]` + `task_signature` |
| patch-planning | `completeness_checklist[phase=patch]` + `success_recipe` + `failure_antipatterns` |
| patch-failed 返回 planner | `failure_antipatterns` |

scenario 索引表自身不直接进 prompt,仅作 retrieve 通路。

### 9.3 注入方式

所有 retrieve 出来的记忆在 prompt 里都必须标注为"参考经验，非当前事实"。
prompt 硬约束：**当前 case 的代码证据若与参考经验冲突，以代码为准**。

---

## 10. 代码落地点

| 文件 | 动作 |
|---|---|
| `src/models/memory.py` | 给 `SharedWorkingMemory` 加 `case_profile` |
| `src/models/long_term_memory.py` | 新建,放 `CodebaseScenario` / `RepairTask` / `TaskCandidate` / `ChecklistItem` / `SuccessRecipeItem` / `FailureAntipatternItem` / `Source` / `ReflectionOutput` |
| `src/memory/__init__.py` | 新建包 |
| `src/memory/db.py` | SQLite 连接、初始化 schema |
| `src/memory/backfill_eval.py` | 扫 eval_results.json / _output.json，回灌 `patch_outcome.json` |
| `src/memory/reflect.py` | CLI，调用 Reflection Agent + 复核 sources + 形态校验 |
| `src/memory/distill.py` | 去重、写 candidates 表 |
| `src/memory/promote.py` | candidates → 主表的跨 case 晋升（N=2）+ scenario 索引派生 |
| `src/memory/retrieve.py` | 单轴 task 检索 + scenario 反查通路 |
| `src/agents/reflection_agent.py` | 新建 agent，SDK 结构化输出，模型 Sonnet 4.6 |
| `src/orchestrator/engine.py` | 起点调用 retrieve；终点写 `patch_outcome.json` 时加 `eval_result=unknown` 占位 |
| `src/main.py` | 串起 profile 初始化与 retrieve 注入 |

---

## 11. 实施顺序

1. 短期记忆扩字段：`case_profile`（不引入 `failed_attempts`）
2. `patch_outcome.json` 加 `eval_result` 占位
3. `backfill_eval.py`：把已有 6 个 case 的 eval 结果回灌
4. `long_term_memory.py` + `db.py`：schema 落地（task 知识表 + scenario 索引表）
5. `reflection_agent.py` + `reflect.py` + `distill.py`：单 case 蒸馏闭环（含形态 validator）
6. 用 6 个 case 先跑一轮，观察 candidates 表产出是否合理
7. `promote.py`（N=2）+ `retrieve.py`
8. `engine.py` / `main.py` 接入 retrieve

---

## 12. 验证

### 12.1 单元

- `backfill_eval` 幂等：同一 batch 跑两次结果一致
- Reflection 输出的 sources 必须可复核：构造一条虚假 source，distill 阶段应丢弃该条目
- 单 case 写入上限：构造 3 条 task_candidates 的输出，应被整份拒绝
- Jaccard 合并：两条相似度 0.8 的经验文本，写入后应合并为一条
- 形态 validator：
  - `checklist[phase=evidence]` 含动词的条目应被丢弃
  - `checklist[phase=patch]` 缺 `↔` 的条目应被丢弃
  - `success_recipe` 含 `"也要"` 或文件路径的条目应被丢弃
- task 晋升时 scenario 索引派生：两个不同 case 贡献同一 task_id，
  晋升后对应 scenario 的 `related_task_ids` 应包含该 task

### 12.2 端到端

用已有 6 个 case：

- 6 个都跑一遍 reflect，观察 candidates 表
- 人工检查 candidates 是否真的是"可迁移经验"，不含 repo 专名和行号
- 检查跨 case 共享的经验是否能在 N=2 时正确晋升,scenario 索引同步更新
- 在第 7 个新 case 起点 retrieve，观察是否能拉回相关记忆

---

## 13. 关键结论

- 短期记忆只加一个字段 `case_profile`，与 phase23 零耦合；
  `failed_attempts` 已砍掉，patch 失败信息不持久化到 working memory
- 长期记忆为"一张知识表（task）+ 一张索引表（scenario）"：
  所有可执行知识集中在 task，scenario 仅提供 repo/子系统反查入口
- 不引入 archetype 字符串系统：检索完全基于 `task_signature` 的 Jaccard 相似度
- `task_id` / `scenario_id` 直接由签名/路径 hash 担任，主键即去重键，
  删除外部 `dedup_key` / `signature_hash` 概念
- `evidence_must_cover` + `typical_coedit_set` 合并为 `completeness_checklist[phase]`,
  字段从 4 个收敛到 3 个
- 三类知识互窜的防御从"prompt 叮嘱"升级为"提问式 schema + 形态 validator",
  在结构层让它们无法混淆
- Eval 结果通过独立的 `backfill_eval` 回灌到 `patch_outcome.json`，
  让长期记忆有真实的成败判据
- Reflection Agent 只产出 task 候选,scenario 行由 promote 阶段自动派生
- Reflection 产出必须带出处，distill 阶段机械复核 + 形态校验
- candidates 缓冲 + N=2 跨 case 晋升 + 使用后回看，是防过拟合的三重机制
- 不引入 embedding / 向量库 / 人工 promote，整条链全自动
