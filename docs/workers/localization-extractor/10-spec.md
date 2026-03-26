# localization-extractor 规格

## 角色

生成候选修改位置与接口到代码映射关系。

## 输入

- `evidence/localization_card.json`（Phase 1）
- `evidence/symptom_card.json`
- 仓库源码树

## 输出

- 更新后的 `evidence/localization_card.json`（下一版本）

## 验收标准

- 必须存在主要候选位置。
- 必要时列出次要候选与联动编辑候选。
- 映射信息包含文件路径、符号、行区间与置信度。
