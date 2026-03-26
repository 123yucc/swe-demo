# Schema 索引

严格 JSON Schema 由 `src/evidence_cards.py` 导出。

## 生成命令

```bash
python scripts/generate_evidence_schemas.py
```

## Card Schemas

- `docs/schemas/symptom_card.schema.json`
- `docs/schemas/localization_card.schema.json`
- `docs/schemas/constraint_card.schema.json`
- `docs/schemas/structural_card.schema.json`

## 组件 Schemas

- `docs/schemas/evidence_source.schema.json`
- `docs/schemas/observed_failure.schema.json`
- `docs/schemas/expected_behavior.schema.json`
- `docs/schemas/entity_reference.schema.json`
- `docs/schemas/candidate_location.schema.json`
- `docs/schemas/constraint.schema.json`
- `docs/schemas/dependency_edge.schema.json`
- `docs/schemas/co_edit_group.schema.json`
- `docs/schemas/card_version.schema.json`

## 真源规则

若 schema 与代码定义不一致，以 `src/evidence_cards.py` 为真源，并重新生成 schema。
