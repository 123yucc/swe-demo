# Phase 1: 核心数据模型与状态定义 (Foundation)

## 目标
建立整个 Evidence-closure-aware repair harness 的基础数据结构。完全不涉及 Agent 的逻辑交互，只定义数据。使用 Pydantic 进行严格的类型约束。

## 技术栈
- Python 3.10+
- `pydantic`

## 开发任务

### 1. 初始化项目结构
- 创建项目根目录。
- 设置虚拟环境并安装 `pydantic` 和 `anthropic` (为后续阶段准备)。
- 创建核心目录结构：
  - `src/`
    - `models/` (存放数据结构)
    - `agents/` (存放后续的 Agent 逻辑)
    - `orchestrator/` (存放状态机逻辑)

### 2. 定义证据卡模型 (`src/models/evidence.py`)
使用 Pydantic 的 `BaseModel` 定义四张核心证据卡。必须包含详细的 Field descriptions，这不仅是为了代码可读性，后续 Claude SDK 也会将其直接解析为 Tool Schema。

- **SymptomCard**:
  - `error_message` (str, 可选): 提取的核心报错摘要。
  - `reproduction_steps` (list[str]): 复现步骤。
  - `expected_behavior` (str): 预期行为。
  - `actual_behavior` (str): 实际行为。
- **ConstraintCard**:
  - `api_restrictions` (list[str]): API 使用限制。
  - `backward_compatibility_rules` (list[str]): 向下兼容规则。
  - `other_constraints` (list[str]): 其他业务限制。
- **LocalizationCard**:
  - `suspected_files` (list[str]): 嫌疑文件路径。
  - `suspected_functions` (list[str]): 嫌疑函数名。
  - `exact_lines` (list[str]): 精确的代码行号或范围。
- **StructuralCard**:
  - `involved_modules` (list[str]): 涉及的模块及依赖关系。

### 3. 定义聚合模型与上下文 (`src/models/context.py`)
- **EvidenceCards**: 
  - 组合上述 4 张卡片作为一个整体类。
- **SessionContext**:
  - `issue_id` (str): 任务 ID。
  - `evidence` (EvidenceCards): 当前收集到的证据状态。
  - `pending_todos` (list[str]): Orchestrator 下发的待办事项。
  - `is_closed` (bool): 默认 False，标志证据链是否闭环。

## 验收标准
- 能够成功实例化一个空的 `SessionContext`，并且可以将其序列化为 JSON 和从 JSON 反序列化。