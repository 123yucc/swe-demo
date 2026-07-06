# 新增 Custom Knowledge 指导书

## 概述

`workdir/long_term_memory/custom_knowledge.json` 存放**普遍性**的 pattern-level 知识。
这些知识在 harness 运行时被检索，用于指导 deep-search 和 patch-planner 避免已知的系统性失败模式。

## 核心原则

1. **普遍性优先**：知识必须适用于一类问题，而非某个具体 case。如果一条知识只对一个 instance 有效，那就是过拟合。
2. **不要事无巨细**：描述 pattern 和 discipline，不要列出具体文件名、行号、或 instance_id。
3. **Trade-off 意识**：每条知识都有适用范围（通过 tags 限定），不要写"万能规则"。

## 工作流程

### 1. 分析评测结果

评测结果位于 `workdir/eval_result/`，每个 instance 目录下有：
- `_output.json` — harness 运行输出（含 patch_outcome）
- `_patch.diff` — 生成的 patch
- `workspace/output.json` — 评测脚本输出（含测试通过/失败详情）
- `workspace/stderr.log` / `workspace/stdout.log` — 测试运行日志

### 2. 归纳失败模式

从失败 case 中提炼出：
- **symptom**：什么样的 problem statement 特征会触发这个失败模式？
- **root cause**：harness 在哪个环节做了错误决策？
- **generalization**：这个模式还会在哪些类似场景出现？

### 3. 撰写知识条目（中文草稿）

先用中文写出条目内容，格式如下：

```
标题：<简短描述这个 pattern>
症状：<什么样的输入/场景会触发>
指导：<应该怎么做，分点列出>
标签：
  - repo_type: <适用的仓库类型，如 go/python/rust/javascript，可为空>
  - task_type: <适用的任务类型，如 api-contract/auth-and-session/data-access/test-and-tooling，可为空>
  - change_shape: <适用的变更形状，如 rename/restructure/add-field/add-endpoint/move-or-extract/config/struct-shape-change，可为空>
```

### 4. 用户确认

将中文草稿展示给用户确认。用户可能会：
- 要求调整范围（太宽/太窄）
- 要求修改措辞
- 要求合并到已有条目
- 直接批准

### 5. 翻译并写入

用户确认后，将内容翻译为英文，调用脚本写入：

```bash
python workdir/long_term_memory/add_knowledge.py add \
    --id "custom-<kebab-case-slug>" \
    --title "English title" \
    --symptom "English symptom description" \
    --guidance "English guidance (use \\n for line breaks)" \
    --repo-type "go,python" \
    --task-type "api-contract" \
    --change-shape "rename,restructure"
```

或者直接让 Claude 在会话中编辑 `custom_knowledge.json`（脚本只是辅助工具，直接编辑 JSON 也可以）。

## Tags 字段说明

tags 用于运行时检索匹配，决定哪些知识条目会被注入到 agent prompt 中。

| 字段 | 类型 | 说明 |
|------|------|------|
| `repo_type` | `list[str] \| null` | 仓库类型（**枚举**，见下）。null 表示不限 |
| `task_type` | `list[str] \| null` | 任务类型（**枚举**，见下）。null 表示不限 |
| `change_shape` | `list[str] \| null` | 变更的代码形状（**枚举**，见下）。null 表示不限 |

> ⚠️ 三个轴都是 `src/models/custom_rules.py` 里的严格 `Literal` 枚举。写入的标签值**必须**来自下面的列表，否则该条目会在 `load_custom_rules()` 里因 pydantic 校验失败而被**静默跳过**（不报错，但运行时永不命中）。写完后务必运行 `python -c "from src.memory.custom_route import load_custom_rules; print(len(load_custom_rules()))"` 确认条目数与预期一致。

### repo_type 枚举值
`web-app`, `web-framework`, `cli-tool`, `library`, `service-platform`,
`data-pipeline`, `language-tooling`

### task_type 枚举值
`auth-and-session`, `data-access`, `api-contract`, `ui-display`,
`config-and-flags`, `business-logic`, `infra-integration`, `test-and-tooling`

### change_shape 枚举值
`add-field`, `add-method`, `add-endpoint`, `move-or-extract`,
`fix-validation`, `fix-state-handling`, `restructure`, `rename`, `config`,
`behavior-correction`, `struct-shape-change`

tags 的匹配逻辑：条目的某个 tag 字段为 null 时表示"不限制"，
当前 case 的属性会与非 null 的 tag 字段做交集匹配。

## 质量检查清单

写入前自查：
- [ ] 这条知识是否适用于至少 3 个以上潜在 case？（不是只对当前失败有效）
- [ ] symptom 描述的是输入特征，不是某个具体仓库的代码细节？
- [ ] guidance 是否可操作（agent 能据此改变行为），而非仅描述问题？
- [ ] tags 是否精准限定了适用范围，既不过宽也不过窄？
- [ ] 是否与已有条目重复或冲突？如果相关，是否应该合并？

## 其他命令

```bash
# 列出所有条目
python workdir/long_term_memory/add_knowledge.py list

# 删除条目
python workdir/long_term_memory/add_knowledge.py remove --id "custom-xxx"
```
