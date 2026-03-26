# constraint-extractor 规格

## 角色

提取必须满足、不得破坏以及类型/接口相关约束。

## 输入

- `evidence/constraint_card.json`（Phase 1）
- requirements/interface artifacts
- 仓库代码与 schema 定义

## 输出

- 更新后的 `evidence/constraint_card.json`（下一版本）

## 验收标准

- API 与兼容性约束必须明确。
- 可识别时必须抽取类型约束。
- 边界情况义务必须列出，或明确标注 unknown。
