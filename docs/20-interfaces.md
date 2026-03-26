# 接口规范

## Phase 1 输入（artifacts）

路径：`workdir/<instance_id>/artifacts/`

- 必需：`problem_statement.md`
- 可选：`requirements.md`
- 可选：`interface.md` 或 `new_interfaces.md`
- 可选：`expected_and_current_behavior.md`
- 可选：`tests/fail2pass/*`
- 可选：`tests/pass2pass/*`

## Evidence Card 规范来源

- 运行时模型真源：`src/evidence_cards.py`
- 严格 JSON Schema：`docs/schemas/*.schema.json`

## 四类 Card 文件

- `evidence/symptom_card.json`
- `evidence/localization_card.json`
- `evidence/constraint_card.json`
- `evidence/structural_card.json`

版本快照目录：

- `evidence/card_versions/v<version>/<card>_v<version>.json`

## 通用字段（四类 Card）

- `version: int`
- `updated_at: str`
- `updated_by: str`
- `sufficiency_status: sufficient|insufficient|partial|unknown`
- `sufficiency_notes: str`

## 子结构字段（关键）

`EvidenceSource`（用于溯源）：

- `source_type`
- `source_path`
- `matching_detail`
- `confidence_contribution`

`SymptomCard` 关键字段：

- `observed_failure`
- `expected_behavior`
- `mentioned_entities`
- `hinted_scope`
- `evidence_sources`
- `missing_artifacts`

`LocalizationCard` 关键字段：

- `candidate_locations`
- `test_to_code_mappings`
- `interface_to_code_mappings`

`ConstraintCard` 关键字段：

- `must_do` / `must_not_break`
- `allowed_behavior` / `forbidden_behavior`
- `compatibility_expectations`
- `edge_case_obligations`
- `constraints`
- `api_signatures` / `type_constraints`
- `backward_compatibility` / `compatibility_notes`

`StructuralCard` 关键字段：

- `dependency_edges`
- `co_edit_groups`
- `propagation_risks`

## 调度状态接口

由 `src/scheduler/models.py` 定义：

- `WorkflowState`：全局工作流状态
- `TaskSpec`：worker 执行记录
- `TodoItem`：动态待办

持久化文件：

- `.workflow/workflow_state.json`
- `plan/todo_queue.json`
- `logs/scheduler_events.jsonl`
