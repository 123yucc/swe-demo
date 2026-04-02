# 文档索引

目标：为 agent 与开发者提供与当前代码一致的最小文档地图。

## 推荐阅读顺序

1. `docs/00-index.md`
2. `docs/05-requirements-analysis.md`
3. `docs/10-architecture.md`
4. `docs/20-interfaces.md`
5. `docs/schemas/00-index.md`
6. `docs/30-naming-rules.md`
7. `docs/workers/00-index.md`
8. `docs/plan/plan.md`

## 核心文档职责

- `docs/05-requirements-analysis.md`
  - 背景、目标、范围、风险。
  - 明确当前已实现能力与后续增强能力。

- `docs/10-architecture.md`
  - 单栈 orchestration 架构（入口、状态机、worker runtime、memory）。
  - 6 阶段 worker 流与关键持久化边界。

- `docs/20-interfaces.md`
  - `workdir/<instance_id>/` 下输入输出路径约定。
  - Evidence Card 与阶段产物接口约束。

- `docs/schemas/00-index.md`
  - 由 `src/evidence_cards.py` 导出的 JSON Schema 索引。

- `docs/30-naming-rules.md`
  - 目录、文件、ID、JSON key、版本命名规则。

- `docs/workers/00-index.md`
  - Worker 清单、职责、依赖与规范链接。

## 计划文档

- `docs/plan/plan.md`：Phase1/Phase2 实现、runtime 接入、无用实现清理计划。

## 参考资料

- `docs/claude_sdk_docs/`：Claude Agent SDK 镜像参考文档。

## 维护原则

- 文档以代码事实为准，优先反映 `main.py` 与 `src/` 当前行为。
- 路径、字段名、状态枚举必须可直接映射到代码。
- 废弃实现或目录删除后，同步更新文档中的路径和术语。
