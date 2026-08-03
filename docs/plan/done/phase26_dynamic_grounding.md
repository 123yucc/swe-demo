# Phase 26: 动态接地 — 复现执行路径以接地根因

phase25 落地了③正确归因的**静态**接地（grep / Read / AST）；本期补上**动态**接地：实际复现 bug、捕获 buggy 代码的真实执行路径，用它给 `symptom.observable_failures` 与 `localization.call_chain_context` 做接地。

## 复现的两类行为

- 自己写脚本**触发 bug、观察当前实际行为**：采用。脚本只把复现步骤跑出来，捕获 buggy 代码的实际执行路径 + 实际异常。
- 自己编测试**断言「修复后应该怎样」**：禁止。不发明验收标准。

边界：脚本只负责触发 bug 并观察；「实际路径是否经过 cited location」是脚本执行 + 机械比对，不让 LLM 定义「对的结果应该是什么」。脚本写错了无非触发不了 / 报无关错误 → 归入 `unverifiable`，不污染证据。

bug 是 `base_commit` 代码里客观存在的缺陷；`problem_statement` 通常自带复现步骤（如 issue_001 的 "Steps to reproduce"），不依赖评估者的 hidden test（`fail_to_pass` / gold test）。

---

## 目标

单次复现 bug，得到 buggy 代码下的实际执行路径 + 实际抛出的异常/堆栈，为两个静态接地填不满的字段做接地：

- `symptom.observable_failures` — 复现成功即证明这是实在的 bug；实际异常类型/消息与卡里声称的症状比对。
- `localization.call_chain_context` — 实测执行路径若经过 cited `evidence_locations`，即证明该位置在根因路径上，而非仅文本相关。

静态/AST 接地补**结构**（边/作用域/定义点存在性）；动态接地补**执行**（运行时是否真的走到 cited location）。

---

## 运行环境（决定每个机制能否真正点亮）

harness 跑在**每个 instance 自己的 SWE-bench Pro docker 镜像内**（`docker run ... --repo-dir /app`），不是宿主机。这个事实约束了整个方案，必须先讲清：

- **每个镜像只自带它那一种语言的工具链**：Go case 镜像有 `go`，Python case 镜像有 `python`，Java case 镜像有 JDK + mvn/gradle，JS case 镜像有 node。`detect_build_system` 对一个 case 只选一种语言分发，所以**任何一个镜像里都不需要凑齐四套工具链**——动态接地和 `build_verify` 一样，永远只用当前 case 那一种语言的原生 runner。该语言的 test runner 必然在场（这正是 `build_verify` 的 Go build gate 能在 009/010/013 跑通、却在宿主机报 `BUILD_UNVERIFIABLE` 的原因）。
- **环境离线**：镜像内 `unset http_proxy`、`NO_PROXY=*`、pip 用 `--no-index --find-links wheels`。运行时**不能** pip/npm/mvn 联网拉任何包。任何「需要现装」的工具都视为不可得。

这条离线约束直接决定了路径捕获的两层分工（见下「怎么捕获执行路径」）：**异常/堆栈解析零依赖、四语言无条件可用，是主信号；覆盖率工具需预装、按语言可得性不同，降为「探测到才用」的机会增强**。两层都拿不到路径 → `unverifiable`。这样方案里每条都真能在 docker 里点亮，绝不写一堆离线跑不起来的分支。

---

## 语言覆盖（与 AST 接地对齐：python / go / java / js）

SWE-bench-Pro 横跨多语言，动态接地的语言面必须跟 `ast_grounding`（已通过 tree-sitter 支持 go/js/ts/java）对齐，否则多语言 case 的运行时接地全是缺口。本期实现 **python / go / java / js 四个复现后端**。

路径捕获分两层，可得性不同（见「运行环境」的离线约束）：

- **堆栈/异常解析（主信号，四语言无条件）**：复现一旦抛异常/panic/测试失败，解析 stderr 的帧序列即得 `[(file, line)]`。零额外依赖，纯文本解析，离线必然可用。
- **覆盖率行命中集（机会增强，探测到才用）**：补「没抛异常但确实执行到」的情形，归一化到 `{file: set[lines]}`。覆盖率工具需预装，按语言可得性差异大；探测不到就退到 trace-only，绝不因缺 coverage 判 `unverifiable`。

每语言的复现来源、堆栈解析、覆盖率可得性：

| 语言 | 构建系统标记 | 复现来源（按能否复现症状排序） | 堆栈/异常解析（主，无条件） | 覆盖率（增强，离线可得性） |
|---|---|---|---|---|
| Python | `pyproject.toml` / `setup.py` / `setup.cfg` | 合成触发脚本（解释型，`python repro.py` 直接可跑） | traceback 帧 `file.py:line` | `coverage.py`：仅当目标 repo 环境已装才用（离线装不进 repo venv） |
| Go | `go.mod` | 合成触发脚本 / 用 problem_statement 触发输入跑某测试 | goroutine stack trace（stderr `file.go:line`） | `-coverprofile`：`go test` 内建，**稳定可得** |
| Java | `pom.xml` / `build.gradle` | 合成触发脚本（`mvn`/`gradle` 编译运行） | JUnit 失败堆栈 `at pkg.Cls.m(File.java:line)` | JaCoCo `jacoco.exec`：**仅当构建已配 agent 才有**，多数没配 → 退 trace-only |
| JS/TS | `package.json` | 合成触发脚本（`node repro.js` / 经 runner） | error stack `at fn (file.js:line:col)` | runner 内置 coverage（jest 自带 istanbul；mocha 看 repo 是否配 nyc/c8）→ 有则用 |

四语言都能由受限 LLM 把 problem_statement 合成触发脚本，没有谁被禁用。差别只在合成「可独立运行」脚本的门槛：Python 解释型最低；Go/Java/JS 要过编译/classpath/模块系统，门槛高、`unverifiable` 比例更大，由静态/AST 兜底。这是语言事实，不是偏好。

---

## 复现脚本如何合成

`problem_statement` 的复现步骤常是自然语言、依赖 UI/服务，纯代码模板拼不出来。本期采用方案 a：允许一个受限 LLM 环节，把复现步骤翻译成可执行脚本。

- LLM 只做一件事：把 `problem_statement` 的复现步骤 + symptom 卡翻译成一个可执行的触发脚本。
- LLM 绝不做：判定接地是否成立、定义正确行为、断言期望结果。
- 接地判定（实际路径是否经过 cited location）始终是脚本执行 + 机械比对。

### 合成的两个 backend（优先「照猫画虎」，回退「凭空脚本」）

合成脚本有两种来源，**优先级 1 > 2**，尤其在 go/java 这类编译/classpath/模块门槛高的语言：

1. **已有测试函数作模板（`existing_test_template`，go/java 首选）**：在 cited `suspect_entities` 同目录下找已有测试文件（`*_test.go` / `*Test.java` / `*.test.js`），复制一个结构最接近的测试函数，**只改其中的输入参数**为 problem_statement 描述的触发输入，用项目原生 runner 跑单个用例（`go test -run TestXxx` / `mvn -Dtest=Cls#m test`）。
   - 为何优先：复用了项目现成的 build 配置、依赖、import、fixture，**不必合成一个能独立编译的 `package main` / 带 classpath 的 Java main**——这正是「凭空脚本」在 go/java 上 `unverifiable` 比例高的根因。模板来自已有测试而非空白文件，能跑通的概率高得多。
   - 边界不变：Agent 只改**输入参数**以触发 buggy 行为，**绝不写「修复后应该怎样」的断言**，也不引用 hidden/gold test。改完的临时测试写临时位置、跑完即清，不进 git 工作树受控路径（同「安全与成本」节）。
   - 失败/同目录无可用测试 → 回退 backend 2。
2. **凭空合成独立触发脚本（`synthetic_script`，python 首选 / 其他兜底）**：无可复制模板时，受限 LLM 把复现步骤翻译成一个独立可执行脚本（python 解释型门槛最低）。失败/拒绝 → None（→ unverifiable）。

两个 backend 都只「触发并观察」，接地判定始终是脚本执行 + 机械比对，与上面的边界一致。

## 症状闸门：强信号的前提是「复现出症状」，不是「执行到」

这是本期最关键的判别标准，直接决定动态接地有没有价值。

**触发 bug 的那个测试是 hidden gold test（`fail_to_pass`），评估者在 eval 时才打补丁加进去，base_commit 里没有。** 所以 base_commit 里**已存在的测试，在 buggy 代码上几乎都是通过的**。驱动一个通过的已有测试：它不抛异常（没有堆栈），coverage 命中 cited location 只证明「这行在某条**正常**路径上能被执行到」—— 这正是 `ast_grounding` 的 call-edge / def-use 已经给过的**可达性**，重复且边际价值极低。可达 ≠ 在失败路径上。

因此强 `dynamic_reached` 的硬前提是：**这次运行真的表现出了 symptom 卡里的 observable failure**（抛出对应异常 / panic / 失败堆栈，类型或消息与症状卡可比对）。只有在「确实复现了症状」的运行里，「路径经过 cited location」才意味着该位置在**根因路径**上。来源是合成脚本还是已有测试是次要的；**没复现出症状的运行，无论 coverage 命中什么都不算接地信号**。

### 复现来源优先级（按「能否复现症状」排，不按「能否跑通」排）

1. **合成触发脚本**（四语言主力，故意触发 bug，最可能产出症状帧）——内部两个 backend，优先 1a：
   1a. **已有测试函数作模板**（`existing_test_template`，go/java 首选）：复制同目录已有测试函数、只改输入参数触发 bug，用原生 runner 跑单用例。复用现成 build/依赖，`unverifiable` 比例最低。
   1b. **凭空独立脚本**（`synthetic_script`，python 首选 / 兜底）：无可复制模板时受限 LLM 翻译复现步骤为独立脚本。
2. **base_commit 里本就标记为已知缺陷的测试**（`xfail` / `skip("known bug")` / 注释指向本 issue）：去掉跳过标记后驱动，这类测试设计上**就该失败**，能产出症状。普通通过测试不在此列。
3. 无法产出「带症状」的运行（两个 backend 均失败、无已知缺陷测试、或所有运行都静默通过）→ `unverifiable`，回退静态/AST。

禁止引用 hidden/gold test（`before_repo_set_cmd` 才 checkout，agent 不可见）。普通通过的 base_commit 测试**不作复现来源**——它接地不了根因，只会用可达性冒充症状。

---

## 三态语义（继承 `build_verify.py`）

动态接地复用 `build_verify` 相同的 subprocess + 还原模式，继承其三态。三态的闸门是「是否复现出症状」（见上节），不是「是否执行到」：

- **强接地通过**（复现出症状 + 实测路径经过 cited location）：`grounded_by="dynamic_reached"`。正向置信信号，喂给 closure-checker 作上下文。
- **接地存疑**（复现出症状 + 路径未经过 cited location）：`grounded_by="dynamic_not_reached"`。软信号——cited location 可能归因错；作为一条 consistency 输入交给 LLM 质疑者，不自动 reset（复现脚本本就可能覆盖不全）。
- **无法验证**（复现脚本跑不起来 / 运行**没表现出任何症状**（如静默通过）/ 工具链缺失 rc=127 / setup 报错 / 无能产出症状的复现来源）：`grounded_by="dynamic_unverifiable_fallback"`。无意见，静态接地结果照旧，不阻断流程。**「执行到但没复现症状」明确归此态，coverage 命中不算数。**

---

## opt-in 触发条件

动态接地是 opt-in，不满足即跳过、回退静态：

- 构建系统 ∈ {python, go, java, node}。unknown 跳过。复用 `build_verify.detect_build_system`（需在本期为 java/node 补全测试驱动分支；node 的纯运行时复现按需活服务与否再降级）。
- `symptom.observable_failures` 含可翻译的复现信息（步骤 / trace / 触发输入），或存在静态可达 cited location 的 base_commit 测试。
- 每 case 仅一次动态接地 pass（不进 per-requirement 循环），执行预算封顶。

需活 DB/服务（如 NodeBB）的复现会在 setup/collect 报错 → `unverifiable`，opt-in 命中但结果不阻断。

---

## 怎么捕获执行路径

两层捕获，主次分明（见「运行环境」离线约束）：

- **主：堆栈/异常帧（无条件）**。复现脚本抛出异常 / panic / 测试失败堆栈时，捕获实际帧序列（文件:行 + 异常/panic 类型与消息）。零依赖、纯解析 stderr，四语言离线都可用。Python = traceback；Go = goroutine stack trace；Java = JUnit `at ...(File.java:line)`；JS = error stack `at fn (file:line:col)`。
- **增强：覆盖率行命中集（探测到才用）**。兜住「没抛异常但确实执行到」的情形，归一化到 `{file: set[lines]}`。Python `coverage.py`、Go `-coverprofile`、Java JaCoCo `jacoco.exec`、JS istanbul/c8 `coverage-final.json` —— 各 adapter 先**探测工具是否可得**（命令存在 / 构建已配 agent），可得才采集，不可得就只用堆栈层，**绝不因缺 coverage 判 `unverifiable`**。
- 判定「经过 cited location」：实测路径（堆栈帧 ∪ 覆盖率行）∩ `exact_code_regions` 行范围，或落在 cited 符号的 AST def-span 内（复用 `ast_grounding` 已有的 def 行范围，四语言都已支持）。用函数粒度而非精确行（抗行号漂移）。
- per-language adapter 与 `build_verify` 按语言分发同构，统一在 `LANG_ADAPTERS` 注册表里：每个 adapter 暴露 `(detect, drive_test, parse_trace, probe_coverage, parse_coverage)` 钩子，新增语言只加一条注册项。`probe_coverage` 返回不可得时，该语言自动走 trace-only。

---

## 安全与成本

- 完全复用 `build_verify` 的 subprocess 模式：严格超时（如 300s）、捕获合并 stdout/stderr、rc=127 → toolchain missing → `unverifiable`。
- 动态接地在**证据阶段**（补丁前）跑的是 `base_commit` 状态。跑完必须还原工作树（`git checkout -- . && git clean -fd`），防复现脚本/测试写脏。覆盖率产物（`.coverage` / coverprofile / `jacoco.exec` / `coverage-final.json`）写临时目录并清理。
- 需服务的复现会在 setup/collect 报错 → `unverifiable`，不会假装跑通。
- 复现脚本写入临时位置，执行后清理，不进 git 工作树的受控路径。

---

## 与静态 / AST 接地的协同

- AST 对「结构可达」权威，动态只补「本次复现是否真走到根因路径」。AST 说「边存在」与动态说「未命中」不矛盾。
- 动态只在**复现出症状**时才比 AST 多出信息（证明 cited location 在失败路径上，而非仅可达）；没复现出症状时动态退回 `unverifiable`，由静态/AST 接地，不冒充信号。
- 冲突时：静态/AST 优先用于门控（决定 reset 与否）；动态分歧降级为交给 LLM 质疑者消费的 note，不自主改流向。
- 只把明确证伪当 fail；动态不因脆弱性硬 fail。

---

## 文件级落地清单

### 1. 新增 `src/orchestrator/dynamic_grounding.py`（代码门控 + 受限 LLM 仅作脚本翻译）

- [ ] `DynamicGroundingResult`：per-requirement 三态结果 + `grounded_by` 标注 + 实测路径/异常摘要。
- [ ] `LANG_ADAPTERS` 注册表：python / go / java / js 四个 adapter，每个暴露 `detect / drive_test / parse_trace / probe_coverage / parse_coverage` 钩子；按 `build_verify.detect_build_system` 的语言分发到对应 adapter。`probe_coverage` 不可得时该语言走 trace-only。
- [ ] `select_reproduction_source(evidence, repo_dir)`：按「能否复现症状」选源——合成触发脚本（四语言主力）；base_commit 中标记为已知缺陷的测试（`xfail`/`skip`，去标记后驱动）。普通通过测试不作来源。都无 → unverifiable。
- [ ] `synthesize_reproduction_script(symptom, problem_statement, language, repo_dir, suspect_entities)`：受限 LLM 环节，两个 backend 按优先级：
  - **1a `existing_test_template`（go/java 首选）**：在 `suspect_entities` 同目录找已有测试文件（`*_test.go`/`*Test.java`/`*.test.js`），复制结构最近的测试函数、只改输入参数触发 bug，产出可由原生 runner 跑单用例的临时测试（`go test -run` / `mvn -Dtest=`）。复用现成 build/依赖，不需独立编译。
  - **1b `synthetic_script`（python 首选 / 兜底）**：无可复制模板时，翻译复现步骤为独立脚本。
  - 系统 prompt 对两个 backend 同样严格限定：只产出触发脚本、只改输入参数、不断言正确行为、不 import hidden test。两者都失败/拒绝 → None（→ unverifiable）。
- [ ] `run_reproduction(repo_dir, source, adapter, timeout)`：subprocess 执行，捕获堆栈帧（主）+ 覆盖率（`probe_coverage` 可得时）；继承 `build_verify` 三态（rc=127 / setup 报错 → unverifiable；缺 coverage 不算 unverifiable）。执行后还原工作树。
- [ ] `observed_symptom(run_result, symptom_card)`：**症状闸门**——判定本次运行是否表现出 symptom 卡的 observable failure（有非零退出 + 异常/panic/失败堆栈，类型或消息与症状卡可比对）。返回 False → 整个结果降级 `unverifiable`，coverage 命中一律不算数。
- [ ] 各语言 `parse_*_trace` / `parse_*_coverage`：解析 traceback / stack / coverage 产物为归一化 `[(file, line)]` 与 `{file: set[lines]}`，与 `build_verify` 同模块风格。
- [ ] `match_path_reached(trace, coverage, cited_regions, ast_index)`：机械比对，语言无关，返回每条 cited location 的 `reached | not_reached`。**仅在 `observed_symptom` 为真时才被调用。**
- [ ] 复用 `audit._parse_evidence_location`、`ast_grounding` 的 def-span，不重复实现。

### 2. `src/orchestrator/build_verify.py` 扩展

- [ ] `detect_build_system` 已识别 go/python/node/unknown；本期为动态接地补 java（`pom.xml` / `build.gradle`）识别，供 `dynamic_grounding` 分发使用（build 门控本身是否扩 java 单独评估，不在本期强求）。

### 3. `src/orchestrator/engine.py` 接线

- [ ] 在静态接地门控之后、closure-checker LLM 之前插入动态接地，opt-in 触发，每 case 一次。
- [ ] `dynamic_reached` → 写入 memory 作正向置信上下文（不改流向）。
- [ ] `dynamic_not_reached` → 注入 closure-checker 一致性输入（不 reset）。
- [ ] `dynamic_unverifiable_fallback` → 标注，无动作，静态结果照旧。
- [ ] 执行前后包好工作树还原；预算耗尽与现有门控一致（放行，不死循环）。

### 4. closure-checker 输入注入（`src/agents/closure_checker_agent.py`）

- [ ] 新增「动态可达性 note」输入段（实测路径是否经过 cited location）。
- [ ] 不改 `format_for_prompt` 契约（守住 `test_requirement_status.py:57`）——走 closure-checker 专用拼装，与 phase25 注入 compliant 群同款方式。

### 5. 统一 `grounded_by` 标注

- [ ] 给静态接地结果也回填 `grounded_by`（`static_grep` / `ast` / `grep_fallback`），与动态侧标注统一，便于事后区分接地来源。

### 6. 依赖（受离线 docker 约束）

镜像内无网络、pip `--no-index`，**运行时不能现装任何工具**。因此覆盖率一律「探测到才用」，不可得就退 trace-only：

- [ ] **不**向 repo 环境强行安装 coverage 工具。Go `-coverprofile` 是 `go test` 内建（稳定可得）；Python `coverage.py` 仅当目标 repo 已依赖才用；Java JaCoCo 仅当构建已配 agent 才有；JS coverage 看 runner（jest 自带 istanbul）。任一不可得 → `probe_coverage` 返回 false → trace-only。
- [ ] harness 自身的 `coverage`（若用于解析 Python repo 的 `.coverage` 文件）走 wheel 预下，纳入 `requirements.lock`，不在镜像内联网装。
- [ ] 工具链整体缺失（rc=127）才 `unverifiable`；缺 coverage 不是 unverifiable。

---

## 明确不做

- 不自己编测试断言「修复后应该怎样」——只观察 buggy 行为，不发明验收标准。
- 不让 LLM 判定接地结果——LLM 仅作复现步骤→脚本的翻译，裁判永远是脚本执行 + 机械比对。
- 不引用 hidden/gold test（agent 不可见）。
- **不拿普通通过的 base_commit 测试当复现来源**——它接地不了根因，coverage 命中只是可达性（AST 已覆盖），会用可达冒充症状。
- **不把「执行到但没复现症状」当 `reached`**——症状闸门不过即 `unverifiable`。
- `not_reached` 不自动 reset——软信号交 LLM，避免复现脆弱性误伤。
- 需活服务（DB/UI）的复现不强行搭环境——setup 报错即 `unverifiable`。

---

## 测试（遵循 CLAUDE.md：禁 mock，真实双向断言）

- [ ] 真实小 Python repo + 真实复现脚本（**会抛症状异常**）：cited location 在实测路径上 → 断言 `dynamic_reached`；不在 → 断言 `dynamic_not_reached` 且未触发 reset。
- [ ] 真实小 Go module + 触发脚本（panic）：路径经过 cited 行 → `dynamic_reached`；未经过 → `dynamic_not_reached` 不 reset。
- [ ] 真实小 Java 项目（Maven 或 Gradle）+ 触发脚本（抛异常）：堆栈经过 cited 行 → `dynamic_reached`；未经过 → `dynamic_not_reached` 不 reset。
- [ ] 真实小 JS 项目 + 触发脚本（throw）：error stack 经过 cited 行 → `dynamic_reached`；未经过 → `dynamic_not_reached` 不 reset。
- [ ] **症状闸门关键用例**：跑一个**静默通过、不抛异常**的 base_commit 测试，其 coverage 命中 cited location → 断言结果是 `dynamic_unverifiable_fallback`（**不**是 `dynamic_reached`），证明可达性不冒充症状。
- [ ] 每语言「CI 无对应 toolchain（go/mvn/gradle/node）时该用例自身走 unverifiable，断言不误判为 fail」——正好双测三态。
- [ ] **缺 coverage 但有症状堆栈**：模拟 `probe_coverage` 返回 false，断言走 trace-only 仍能判 `dynamic_reached`/`dynamic_not_reached`，**不**因缺 coverage 判 `unverifiable`。
- [ ] 制造工具链缺失 / setup 报错 / 无合格复现来源 → 断言 `dynamic_unverifiable_fallback`，静态结果不变。
- [ ] 复现脚本翻译失败（None）→ 断言降级 unverifiable，不阻断。
- [ ] 工作树还原断言：动态接地跑完后 `git status` 与跑前一致（无残留脏文件），覆盖率临时产物被清理。
- [ ] engine 端到端：opt-in 命中（python / go / java / js）vs 不命中（unknown repo 跳过）各路径。

---

## 建议实施顺序（MVP）

1. [ ] `DynamicGroundingResult` + 三态语义骨架 + `LANG_ADAPTERS` 注册表接口。
2. [ ] `observed_symptom` 症状闸门 + `match_path_reached`（仅症状闸门为真时调用；trace/coverage ∩ cited region，机械比对，语言无关）。
3. [ ] `run_reproduction` Python 后端（合成脚本执行 + 症状判定 + coverage 探测 + 三态 + 工作树还原），复用 `build_verify` 模式。
4. [ ] Go 后端（触发脚本 / panic + goroutine stack 解析 + `-coverprofile` 探测 + 清理）。
5. [ ] Java 后端（触发脚本 + JUnit 堆栈解析 + JaCoCo 探测）。
6. [ ] JS 后端（触发脚本 + error stack 解析 + istanbul coverage 探测）。
7. [ ] `select_reproduction_source`（按能否复现症状选源）+ `synthesize_reproduction_script`（受限 LLM 翻译，四语言通用）。
8. [ ] engine 接线（静态之后、closure 之前，opt-in，每 case 一次，前后还原）。
9. [ ] closure-checker 输入注入 + 统一 `grounded_by` 标注。
10. [ ] 测试（上节全部）。
11. [ ] 回归冒烟（开发期抽样，非全量）：每语言抽几个已通过 case，确认动态接地不误伤已通过 case。按语言/类别抽样，不针对特定 case 编号调参。

---

## 决策记录

- **复现 bug 不依赖 hidden test**：bug 是 base_commit 客观缺陷，problem_statement 自带复现步骤；hidden test 仅供评估者打分。
- **只观察不断言**：写脚本触发并观察 buggy 实际行为（采用）；编测试断言正确行为（禁止）。幻觉边界 = 不让 LLM/脚本定义「对的结果」。
- **方案 a：受限 LLM 翻译复现步骤**：自然语言/UI 步骤纯模板拼不出。LLM 只生成脚本，接地判定纯机械。
- **合成优先「照猫画虎」已有测试模板**：go/java 凭空合成可独立编译的脚本门槛高（classpath/模块/依赖），`unverifiable` 比例大；优先复制同目录已有测试函数、只改输入参数触发 bug、用原生 runner 跑单用例，复用现成 build 上下文，能跑通概率高得多。仅在无可复制模板时回退凭空独立脚本（python 首选）。模板法不改变「只触发不断言」边界——只改输入，不写期望结果。
- **三态严格继承 `build_verify`**：跑不起来 = `unverifiable`，绝不当通过、也不当 `not_reached`。
- **离线 docker 是硬约束**：harness 跑在 instance 镜像内（`--repo-dir /app`），每镜像只一种语言工具链、运行时无网络（`--no-index` / `NO_PROXY=*`）。故 `build_verify` 与动态接地都只用当前 case 那一种语言的原生 runner，不需任何镜像凑齐四套工具链；不联网现装任何工具。
- **堆栈为主、覆盖率为机会增强**：异常/堆栈解析零依赖、四语言无条件可用，是接地主信号；coverage 工具按语言离线可得性不同（Go 内建稳定，Java JaCoCo 多半缺，Python/JS 看 repo），`probe_coverage` 探测到才用，缺了退 trace-only，绝不因缺 coverage 判 `unverifiable`。
- **症状闸门是强信号的唯一前提**：触发 bug 的测试是 hidden gold test，base_commit 里没有，已存在的测试在 buggy 代码上几乎都通过。驱动通过测试只能证明可达性（AST 已给），不能接地根因。只有「运行表现出 symptom 卡的 observable failure」时，路径经过 cited location 才意味着在失败路径上。没复现出症状 → `unverifiable`，coverage 命中不算数。
- **不拿普通通过测试当复现来源**：复现来源按「能否复现症状」排——合成触发脚本（四语言主力）、base_commit 中标记为已知缺陷的测试（xfail/skip）。普通通过测试出局。
- **合成脚本对四语言都可用**：没有谁被禁用，差别只在合成可独立运行脚本的门槛（Python 解释型最低，go/java/js 高 → `unverifiable` 比例更大，静态/AST 兜底）。
- **`not_reached` 永不自动 reset**：复现覆盖不全是常态，只作 LLM 软输入。
- **证据阶段执行 + 强制还原工作树**：与 PatchVerifying（补丁后）职责分离；动态接地（补丁前验根因路径）跑完必须还原 base_commit 状态。
- **四语言并列（python / go / java / js）**：SWE-bench-Pro 多语言，动态接地语言面与 `ast_grounding`（tree-sitter 已支持 go/js/ts/java）对齐才能覆盖多语言 case；codebase（build_verify、AST、eval docker）的 Go 支持已成熟。合成脚本对四语言通用，能产出强信号的工作主力是故意触发 bug 的脚本；go/java/js 因编译/classpath/模块门槛高，动态接地落 `unverifiable` 的比例更大，由静态/AST 兜底——动态接地是机会性增强，不是普适闸门。adapter 注册表化，后续加语言只加一条注册项。
