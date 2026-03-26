# structural-extractor 规格

## 角色

分析目标位置周边的依赖关系与协同编辑结构。

## 输入

- `evidence/structural_card.json`（Phase 1）
- `evidence/localization_card.json`（Phase 2）
- 仓库源码树

## 输出

- 更新后的 `evidence/structural_card.json`（下一版本）

## 验收标准

- 列出直接依赖关系。
- 明确协同编辑要求。
- 识别影响范围与传播风险。
