# symptom-extractor 规格

## 角色

从测试、日志与仓库信号中增强 symptom 证据。

## 输入

- `evidence/symptom_card.json`（Phase 1）
- 实例工作区内的 tests/logs/code

## 输出

- 更新后的 `evidence/symptom_card.json`（下一版本）

## 验收标准

- 触发条件明确。
- 观察到的失败与预期行为具备依据。
- 结论包含证据来源与置信度。
