# 命名规则

## 文件与目录命名

- 顶层文档：`NN-name.md`（两位数字前缀）。
- Worker 规范：`docs/workers/<worker-name>/10-spec.md`。
- Worker 名：`kebab-case`（例如 `artifact-parser`）。
- Card 文件：`snake_case`，后缀固定 `_card.json`。

## 工作目录约定

- 实例目录：`workdir/<instance_id>/`
- Card 版本：`evidence/card_versions/v<integer>/`
- 测试工件：`artifacts/tests/fail2pass/` 与 `artifacts/tests/pass2pass/`

## JSON Key 与枚举

- JSON key 使用 `snake_case`。
- 枚举值使用小写英文（如 `sufficient`, `in_progress`）。
- `sufficiency_status` 仅允许：
  - `sufficient`
  - `insufficient`
  - `partial`
  - `unknown`

## ID 规则

- `instance_id`：稳定且可追踪。
- `todo_id`：建议带来源前缀（如 `todo_gap_*`, `todo_fix_*`）。
- `worker_id`：必须与 `create_default_registry()` 一致。

## 版本规则

- Card 每次写回 `version` 递增。
- 版本快照文件名需与 card 版本一致。
- 禁止覆盖历史版本文件。
