# Phase1/Phase2 实现与收敛执行计划

## 1. 目标与范围

本计划用于完成三件事：

1. 落地可运行的 Phase1 与 Phase2 执行实现。
2. 将 Phase1/Phase2 严格接入 orchestration runtime，保证失败即失败，不做降级成功。
3. 清理当前仓库中的历史无用实现、失效映射和兼容残留，收敛为单栈实现。

本计划只覆盖后端执行链路，不包含前端可视化。


## 2. 现状问题

1. orchestration 已成为主执行栈，但 runtime 仍指向历史模块路径，导致导入失败。
2. WorkerSpec 声明与可执行函数未完全对齐，存在“有规格无实现”。
3. 历史残留（旧导出、旧文档描述、旧测试断言）会干扰维护。


## 3. 实施原则

1. 单一执行栈：只保留 orchestration，不回退 orchestrator。
2. 失败可审计：执行器缺失、导入失败、运行异常均返回失败。
3. 协议先行：先固定 contracts，再实现 worker 执行器。
4. 低耦合：worker 只做单阶段能力，状态迁移由 orchestrator 决策。


## 4. Phase1 详细实现

### 4.1 目标

输入 artifacts，生成四类 v1 证据卡与 phase1 summary。

### 4.2 新增或恢复文件

1. src/workers/phase1_artifact_parser.py
2. src/workers/io_artifacts.py（可选，若需要拆出文件读写）

### 4.3 关键函数设计

1. run_phase1_parsing(workspace_dir, instance_id) -> dict
2. parse_artifacts(artifacts_dir) -> dict
3. build_v1_cards(parsed_payload) -> dict
4. write_phase1_outputs(instance_dir, cards, summary) -> None

### 4.4 输入输出约定

输入目录：

1. workdir/<instance_id>/artifacts/problem_statement.md（必需）
2. workdir/<instance_id>/artifacts/requirements.md（可选）
3. workdir/<instance_id>/artifacts/interface.md 或 new_interfaces.md（可选）
4. workdir/<instance_id>/artifacts/expected_and_current_behavior.md（可选）

输出目录：

1. workdir/<instance_id>/evidence/symptom_card.json
2. workdir/<instance_id>/evidence/localization_card.json
3. workdir/<instance_id>/evidence/constraint_card.json
4. workdir/<instance_id>/evidence/structural_card.json
5. workdir/<instance_id>/evidence/card_versions/v1/*
6. workdir/<instance_id>/evidence/phase1_summary.json

### 4.5 业务逻辑拆分

1. Artifact 存在性检查与缺失清单生成。
2. 对 problem_statement 的故障语义抽取（失败描述、触发条件、错误摘要）。
3. 对 requirements/interface 的约束与接口语义抽取。
4. 生成 v1 sufficiency 结论（sufficient/partial/insufficient）。
5. 所有输出写盘并进行最小字段完整性校验。

### 4.6 失败条件

1. artifacts 根目录缺失。
2. problem_statement 缺失。
3. 输出卡片写入失败。
4. 结构化结果缺少必需字段。


## 5. Phase2 详细实现

### 5.1 目标

基于 v1 卡片与 repo 内容，完成四类增强抽取并写入 v2。

### 5.2 新增或恢复文件

1. src/workers/phase2_symptom_extractor.py
2. src/workers/phase2_localization_extractor.py
3. src/workers/phase2_constraint_extractor.py
4. src/workers/phase2_structural_extractor.py
5. src/workers/phase2_enhancer.py（聚合增强，可选）

### 5.3 各 worker 输入输出

Symptom Extractor：

1. 输入：symptom_card v1、artifacts、日志/测试（若存在）。
2. 输出：symptom_card v2、sufficiency notes。

Localization Extractor：

1. 输入：symptom_card、repo 文件树。
2. 输出：localization_card v2（候选位置、置信度、映射）。

Constraint Extractor：

1. 输入：constraint_card v1、requirements、interface、代码类型信息。
2. 输出：constraint_card v2（类型约束、兼容性约束）。

Structural Extractor：

1. 输入：localization_card v2、repo AST/引用关系。
2. 输出：structural_card v2（依赖边、联动编辑组、传播风险）。

LLM Enhancer：

1. 输入：四类 v2 草稿。
2. 输出：一致性增强后的四卡最终版本。

### 5.4 输出约定

1. evidence/<card>_card.json 更新为 v2。
2. evidence/card_versions/v2/<card>_card_v2.json 持久化。
3. phase2 summary 文件写入 evidence/phase2_summary.json。

### 5.5 失败条件

1. phase1 输出不存在。
2. repo 目录不存在或无法读取。
3. 关键抽取步骤异常（AST 解析、定位映射、约束构建）。
4. v2 输出 schema 校验失败。


## 6. Runtime 接入计划

### 6.1 Worker Runtime 映射收敛

文件：src/orchestration/worker_runtime.py

1. artifact-parser -> src.workers.phase1_artifact_parser:run_phase1_parsing
2. symptom-extractor -> src.workers.phase2_symptom_extractor:extract_symptom_evidence
3. localization-extractor -> src.workers.phase2_localization_extractor:extract_localization_evidence
4. constraint-extractor -> src.workers.phase2_constraint_extractor:extract_constraint_evidence
5. structural-extractor -> src.workers.phase2_structural_extractor:extract_structural_evidence
6. llm-enhancer -> src.workers.phase2_enhancer:enhance_all_cards

约束：

1. 映射缺失直接失败。
2. 导入失败直接失败。
3. 执行异常直接失败。

### 6.2 WorkerSpec 对齐

文件：src/workers/registry.py

1. executor 字段与 runtime 映射函数名保持一一对应。
2. phase 与 depends_on 与状态机路径一致。
3. 统一 allowed_tools 与 prompt_template 的最小可用定义。

### 6.3 Orchestrator 循环接入点

文件：src/orchestration/orchestrator.py

1. ready_workers 由 depends_on + completed 决定。
2. 每次执行后更新 worker_status 与 worker_results。
3. 仅当满足状态机条件时允许 phase 迁移。
4. validator 完成且无失败后才能进入 patch_success。

### 6.4 CLI 接入

文件：src/app/cli.py

1. dynamic 入口统一调用 pipelines.run_repair_workflow。
2. 输出统一展示 success/final_state/failed_workers。


## 7. 无用实现清理计划

### 7.1 代码清理

1. 删除失效模块路径引用（例如已不存在的 artifact_parsers_llm 等旧路径）。
2. 删除未被 runtime 使用的历史兼容导出。
3. 删除仅为旧 orchestrator 服务的测试分支。

### 7.2 文档清理

1. docs 中所有 scheduler/orchestrator 旧描述统一改为 orchestration。
2. 架构图与目录图同步到单栈版本。

### 7.3 脚本清理

1. scripts/verify_modules.py 移除对旧 orchestrator 的断言。
2. 增加 phase1/phase2 最小冒烟验证函数。


## 8. 分阶段落地顺序

### 阶段 A：实现恢复

1. 补齐 phase1 执行器。
2. 补齐 phase2 四个提取器与增强器。
3. 本地单测每个执行器可独立运行。

### 阶段 B：runtime 接入

1. 修改 worker_runtime 映射到新执行器。
2. 调整 registry 的 executor 与 phase 依赖。
3. 跑一次全链路 dynamic 冒烟。

### 阶段 C：清理收敛

1. 删除失效导入与兼容残留。
2. 修正文档与验证脚本。
3. 再次跑全量错误检查与命令回归。


## 9. 验收标准

1. python main.py <instance_id> --dynamic 可以进入 phase1、phase2 并产出证据文件。
2. 任一执行器缺失或异常时，流程必须失败且 failed_workers 可见。
3. evidence 目录下可看到 v1 与 v2 版本文件。
4. src 中不再存在对已删除实现的导入引用。
5. docs 中不再出现与单栈事实冲突的旧描述。


## 10. 风险与缓解

1. 风险：Phase2 抽取复杂度高，短期难一次到位。
	缓解：先交付最小可运行版，再逐步增强 AST 与置信度策略。

2. 风险：严格失败策略导致早期回归频繁中断。
	缓解：增加每个 worker 的独立冒烟，提前暴露映射错误。

3. 风险：文档与实现再次漂移。
	缓解：每次改 runtime 映射时同步更新 plan 与 architecture 文档。
