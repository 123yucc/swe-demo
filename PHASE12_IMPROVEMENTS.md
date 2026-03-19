# Phase 1/2 改进说明

## 概述

根据需求描述02的改进要求，本项目已完成Phase 1和Phase 2的重构，主要改进点如下：

## Phase 1: Artifact Parsing (LLM驱动版本)

### 主要改进

1. **从硬编码到LLM驱动**
   - 原实现：使用正则表达式硬编码提取
   - 新实现：使用Claude Agent SDK的LLM进行结构化提取

2. **结构化输出**
   - 使用 `output_format` 配合 JSON Schema 获取类型安全的结果
   - 定义了 `PHASE1_OUTPUT_SCHEMA` 规范输出格式

3. **EvidenceSource 溯源**
   - 每个提取的实体都包含 `evidence_source` 列表
   - 记录来源类型、路径、匹配细节和置信度贡献

4. **动态置信度计算**
   - 来源权重：interface > requirement > problem_statement > heuristic
   - 匹配质量：exact > strong > fuzzy > weak
   - 计算公式：`confidence = source_weight * match_quality * context_factor`

5. **文件结构**
   - `src/artifact_parsers_llm.py`: LLM驱动的Phase 1实现

## Phase 2: Evidence Extraction (动态版本)

### 主要改进

1. **SymptomExtractor**
   - 动态错误模式提取（从代码/日志中搜索）
   - Stack trace定位（使用AST分析）
   - 实体增强（在代码库中查找定义）

2. **LocalizationExtractor**
   - 文本grep搜索 + AST分析
   - 装饰器模式识别（@app.route等）
   - 动态置信度：`ast(0.95) > ast_decorator(0.9) > grep(0.7) > heuristic(0.5)`
   - 自动生成 interface_to_code_mappings

3. **ConstraintExtractor**
   - 从代码中提取约束：装饰器、assert语句、docstring
   - 类型约束提取：函数签名、Pydantic/dataclass模型
   - Schema对比（requirements vs code）

4. **StructuralExtractor**
   - 调用图分析（AST caller-callee关系）
   - 导入关系识别（grep import语句）
   - 协同编辑组识别（强依赖关系 + 同文件位置）
   - 传播风险评估

### 文件结构

- `src/evidence_extractors_phase2.py`: Phase 2动态提取器实现
  - `CodebaseNavigator`: 代码库导航器（grep + AST）
  - `DynamicSymptomExtractor`: 动态症状提取
  - `DynamicLocalizationExtractor`: 动态定位提取
  - `DynamicConstraintExtractor`: 动态约束提取
  - `DynamicStructuralExtractor`: 动态结构提取

## Evidence Cards 改进

### 新增字段

1. **EvidenceSource 模型**
   - `source_type`: artifact/repo/test/ast/llm
   - `source_path`: 来源文件路径
   - `matching_detail`: 匹配细节
   - `confidence_contribution`: 置信度贡献

2. **动态置信度字段**
   - `computed_confidence`: 计算后的置信度 (0-1)

3. **版本信息**
   - `updated_by`: 更新者(agent名称)
   - 版本历史保存在 `evidence/card_versions/v{N}/`

### 卡片版本

- **v1**: Phase 1生成（初步提取）
- **v2**: Phase 2生成（增强分析）

## Orchestrator 改进

1. **Agent定义更新**
   - 详细的prompt说明每个agent的任务
   - 分阶段工具限制：Phase 1 (Read/Glob/Write), Phase 2 (Read/Grep/Glob/Bash)

2. **阶段化options**
   - `create_options(phase)`: 支持 "phase1", "phase2", "full"

3. **Hooks配置**
   - `get_hooks_config()`: 预留PreToolUse/PostToolUse钩子

## 使用方法

### 运行Phase 1 (LLM驱动)

```bash
python main.py face_recognition_issue_001 --workspace workdir --phase1-only
```

### 运行Phase 2 (动态提取)

```bash
python main.py face_recognition_issue_001 --workspace workdir --phase2-only
```

### 运行完整工作流

```bash
python main.py instance_001 --problem "Fix the authentication bug..."
```

## 依赖

```
pydantic>=2.0.0
claude-agent-sdk>=0.1.0
```

## 与需求描述的对应关系

| 需求描述02要求 | 实现位置 |
|--------------|---------|
| Phase 1 LLM结构化提取 | `artifact_parsers_llm.py` |
| Symptom Frame提取 | `LLMArtifactParser.parse_with_llm()` |
| Constraint Frame提取 | `LLMArtifactParser._build_evidence_cards()` |
| Localization Anchors | `DynamicLocalizationExtractor` |
| Phase 2 动态Symptom提取 | `DynamicSymptomExtractor` |
| Phase 2 动态Localization | `DynamicLocalizationExtractor` (grep + AST) |
| Phase 2 动态Constraint | `DynamicConstraintExtractor` (装饰器/Schema) |
| Phase 2 动态Structural | `DynamicStructuralExtractor` (caller-callee/import) |
| 动态置信度计算 | `_compute_confidence_v2()`, `_compute_location_confidence()` |
| EvidenceSource溯源 | `EvidenceSource` 模型 |
| 去除硬编码模式 | 所有提取器改为动态分析 |

## 注意事项

1. **Phase 1** 需要Claude Agent SDK (`ANTHROPIC_API_KEY`环境变量)
2. **Phase 2** 使用Python AST分析，需要rg (ripgrep) 或自动fallback到Python实现
3. 所有硬编码模式（如"enroll_batch -> FaceDetector"）已被移除，改为实际代码分析

设置sdk需要：
1. 从 Claude Console 获取 API 密钥。
2. 在你的项目目录中创建一个名为 .env 的文件。
3. 在 .env 文件中添加以下内容：
   
   ```
   ANTHROPIC_API_KEY=your-api-key
   ANTHROPIC_API_BASE=your_base_url
   ```
   将 your-api-key 替换为你从 Claude Console 获取的实际 API 密钥。若需要，your_base_url替换成中转站url。
文档中还提到，如果出现 "API key not found" 的错误，需要确保在 .env 文件或 shell 环境中设置了 ANTHROPIC_API_KEY 环境变量。