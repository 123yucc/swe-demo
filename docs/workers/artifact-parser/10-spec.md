# artifact-parser 规格

## 角色

在 Phase 1 解析 artifacts，并初始化全部 evidence cards。

## 输入

- `artifacts/problem_statement.md`
- `artifacts/requirements.md`（可选）
- `artifacts/interface.md|new_interfaces.md`（可选）
- `artifacts/expected_and_current_behavior.md`（可选）

## 输出

- `evidence/symptom_card.json`
- `evidence/localization_card.json`
- `evidence/constraint_card.json`
- `evidence/structural_card.json`

## 验收标准

- 四张 card 文件全部存在。
- 信封字段完整且合法。
- 缺失 artifact 必须体现在 `sufficiency_notes` 中。
