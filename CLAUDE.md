# CLAUDE.md

本文档是仓库文档的精简地图。
目标：帮助 agent 以最小上下文开销快速定位正确文档。

## 阅读顺序

1. `docs/00-index.md`
2. `docs/plan`
3. `docs/05-requirements-analysis.md`
4. `docs/10-architecture.md`
5. `docs/20-interfaces.md`
6. `docs/schemas/00-index.md`
7. `docs/30-naming-rules.md`
8. `docs/workers/00-index.md`

## 各文档职责

- `docs/00-index.md`
	- 顶层目录与导航入口。
	- 提供架构、接口、命名规则与 worker 规范的跳转点。
	- 指导 agent 在新任务中优先阅读哪些文档。

- `docs/05-requirements-analysis.md`
	- 需求背景、目标、范围与 agentic 设计原则。
	- 功能与非功能需求基线。
	- 阶段验收标准、风险与需求到实现映射。

- `docs/10-architecture.md`
	- 系统结构：6 阶段流程与 worker 职责。
	- 依赖方向与所有权边界。
	- 精简但可执行的架构不变量。

- `docs/20-interfaces.md`
	- artifacts、evidence cards 与流水线输出的规范接口。
	- card JSON 的字段级必需约束。
	- 各阶段输入/输出路径约定。

- `docs/30-naming-rules.md`
	- 文件与目录命名语法。
	- worker 与 phase 的命名规范。
	- JSON key、符号命名与版本规则。

- `docs/schemas/00-index.md`
	- 由运行时模型生成的严格 JSON Schema 索引。
	- card schema 与共享组件 schema 的规范链接。
	- 重新生成路径与真源规则。

- `docs/workers/00-index.md`
	- worker 目录总览。
	- 每个 worker 的一句话目标说明。
	- 指向各 worker 规范的稳定链接。

- `docs/workers/artifact-parser/10-spec.md`
	- Phase 1 解析器契约。
	- 输入、输出与完成标准。

- `docs/workers/symptom-extractor/10-spec.md`
	- 症状证据增强契约。
	- 触发条件/错误提取与充分性检查目标。

- `docs/workers/localization-extractor/10-spec.md`
	- 候选编辑位置契约。
	- 映射与置信度生成职责。

- `docs/workers/constraint-extractor/10-spec.md`
	- 约束提取契约。
	- 兼容性与类型边界义务。

- `docs/workers/structural-extractor/10-spec.md`
	- 结构依赖分析契约。
	- 联动编辑与影响范围预期。

- `docs/workers/closure-checker/10-spec.md`
	- 证据闭环判定契约。
	- patch 规划前置门槛标准。

- `docs/workers/patch-planner/10-spec.md`
	- 规划生成契约。
	- 编辑顺序、风险控制与验证清单。

- `docs/workers/patch-executor/10-spec.md`
	- patch 实施契约。
	- 变更范围控制与变更后验证产物。

- `docs/plan/plan.md`
	- Long/Short Memory 管理设计
	- 现在要实现。

- `docs/plan/plan_2.md`
	- LLM增强与Navigator协同
	- 现在要实现。

- `docs/plan/plan_3.md`
	- 动态调度与Todo编排改造
	- 现在要实现。

## 采用该布局的原因

- 文档短小，交叉链接明确。
- 文件名使用数字前缀，便于确定性检索。
- 接口与命名规则集中管理，降低架构漂移。
- 运行时模型与 JSON Schema 绑定，减少文档与代码漂移。

## 运行注意事项

- 脚本中优先使用 bash 兼容命令。
- 在 bash 测试流程中避免使用仅 Windows 支持的删除命令。
- 始终用中文回复用户。

