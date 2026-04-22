# Phase R5: 机械性验证前置 + 迭代控制

## 目标
将 closure-checker prompt 中的机械性规则提取为代码预检，LLM 只做语义判断；增加迭代计数防止无限循环。

## 改动
- 新增 `src/orchestrator/guards.py`：
  - `check_mechanical_closure(evidence) -> list[str]`：检查 exact_code_regions 非空、suspect_entities 有文件+函数、observable_failures 非空等，返回不满足的字段列表
  - `check_evidence_format(evidence) -> list[str]`：校验 exact_code_regions 格式合法性
- engine.py 中在 dispatch closure-checker **之前**调用 `check_mechanical_closure`，不通过则直接回 deep-search，跳过 LLM 调用
- 新增 `DeepSearchBudget` 类：跟踪 deep-search 调用次数，上限默认 5 次；超限后强制进入 closure-checker 并标注 budget_exhausted
- closure-checker prompt 精简，移除已由代码保证的规则

## 验收
- 机械性不满足时不会浪费 LLM 调用
- deep-search 不会无限循环
