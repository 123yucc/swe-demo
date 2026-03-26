# 文档索引

目标：为 agent 与开发者提供“与当前代码一致”的最小文档地图。

## 推荐阅读顺序

1. `docs/00-index.md`
2. `docs/05-requirements-analysis.md`
3. `docs/10-architecture.md`
4. `docs/20-interfaces.md`
5. `docs/schemas/00-index.md`
6. `docs/30-naming-rules.md`
7. `docs/workers/00-index.md`
8. `docs/plan/`

## 核心文档职责

- `docs/05-requirements-analysis.md`
  - 问题背景、目标、范围、风险。
  - 区分“当前已实现能力”和“目标能力”。

- `docs/10-architecture.md`
  - 当前系统结构（入口、调度器、Memory、LLM 增强层）。
  - 阶段职责、依赖方向、状态与持久化边界。

- `docs/20-interfaces.md`
  - `workdir/<instance_id>/` 下输入输出路径约定。
  - Evidence Card 字段契约与产物格式约束。

- `docs/schemas/00-index.md`
  - 由 `src/evidence_cards.py` 导出的 JSON Schema 索引。
  - 生成脚本与“真源（source of truth）”声明。

- `docs/30-naming-rules.md`
  - 目录、文件、ID、JSON Key、版本命名规则。

- `docs/workers/00-index.md`
  - Worker 清单、职责、依赖与规范链接。

## 计划文档

- `docs/plan/plan.md`: Long/Short Memory 管理设计
- `docs/plan/plan_2.md`: LLM 增强与 Navigator 协同
- `docs/plan/plan_3.md`: 动态调度与 Todo 编排改造

## 参考资料

- `docs/claude_sdk_docs/`：Claude Agent SDK 的镜像参考文档。

## 维护原则

- 文档以“代码事实”为准，优先反映 `main.py` 与 `src/` 当前行为。
- 设计意图可写，但必须与“已实现状态”显式分层。
- 路径、字段名、状态枚举必须可直接映射到代码。
