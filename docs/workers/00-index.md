# Worker 目录

Worker 文档描述各阶段执行单元的契约边界。

## Worker 列表（按当前注册表）

- `artifact-parser`：`docs/workers/artifact-parser/10-spec.md`
- `symptom-extractor`：`docs/workers/symptom-extractor/10-spec.md`
- `localization-extractor`：`docs/workers/localization-extractor/10-spec.md`
- `constraint-extractor`：`docs/workers/constraint-extractor/10-spec.md`
- `structural-extractor`：`docs/workers/structural-extractor/10-spec.md`
- `closure-checker`：`docs/workers/closure-checker/10-spec.md`
- `patch-planner`：`docs/workers/patch-planner/10-spec.md`
- `patch-executor`：`docs/workers/patch-executor/10-spec.md`
- `validator`：`docs/workers/validator/10-spec.md`

## Worker 契约模板

每个 spec 至少包含：

- role（职责）
- inputs（输入）
- outputs（输出）
- acceptance criteria（完成标准）
- failure modes（失败模式）
