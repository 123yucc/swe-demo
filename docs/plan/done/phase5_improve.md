升级版架构计划 (Evidence-Gathering Harness)

（prompt、description都用英文写）
核心设计思想变更
职责收敛： Orchestrator 当前阶段的终态不再是 READY_FOR_PATCH，而是 READY_FOR_CLOSURE_CHECK。它的核心循环是 UnderSpecified <-> Evidence Refining，触发条件从“逻辑未闭环”改为“存在尚未探索的盲区（Evidence still missing）”。

字段升维： 4 张证据卡的字段将严格对齐软件工程的程序分析（Program Analysis）概念，从简单的“行号/报错”升级为“控制流/数据流/约束条件”。

Phase 1: 证据卡模型升维 (Advanced Foundation)
我们需要用 Pydantic 重新定义这四张卡片，让它们能够承载更复杂的分析结果。

Python
# src/models/evidence.py
from pydantic import BaseModel, Field
from typing import List, Optional

class SymptomCard(BaseModel):
    observable_failures: List[str] = Field(description="Issue 描述、报错现象、异常类型、Stack Trace 等可见错误。")
    repair_targets: List[str] = Field(description="修复后应该表现成什么样（Expected Behavior）。")
    regression_expectations: List[str] = Field(description="哪些现有的正确行为绝对不能被破坏。")

class LocalizationCard(BaseModel):
    suspect_entities: List[str] = Field(description="嫌疑的文件、类、函数或符号名。")
    exact_code_regions: List[str] = Field(description="精确的代码行或 Hunks。")
    call_chain_context: List[str] = Field(description="嫌疑位置的调用链邻域（Caller-Callee 关系）。")
    dataflow_relevant_uses: List[str] = Field(description="相关变量的定义点和使用点（Def-Use 关系）。")

class ConstraintCard(BaseModel):
    semantic_boundaries: List[str] = Field(description="需求声明、API 契约 (Contracts)、文档字符串/注释中的约束。")
    behavioral_constraints: List[str] = Field(description="断言 (Assertions)、不变量、类型提示或 Schema 限制。")
    backward_compatibility: List[str] = Field(description="向后兼容性期望。")
    similar_implementation_patterns: List[str] = Field(description="当前代码库中类似 API 的既有实现模式（作为参考基准）。")

class StructuralCard(BaseModel):
    must_co_edit_relations: List[str] = Field(description="必须一起修改的位置（例如改了接口A，必须改调用方B）。")
    dependency_propagation: List[str] = Field(description="接口/包装器/适配器之间的传导关系，或 Config 与 Code 的联动关系。")
Phase 2: Parser Agent (初始化与意图提取)
(与之前方案类似，但需适配新的 Pydantic 字段)

核心任务： 将原始的 MD 文档映射到高维度的证据卡中。例如，它需要能从 problem_statement.md 中不仅提取出报错（填入 observable_failures），还要敏锐地提取出“不能影响现有接口”（填入 regression_expectations）。

Phase 3: Deep Search Agent (多维侦探)
设计转变： 以前的 Deep Search 只是“找代码在哪里”。现在的 Deep Search 必须具备程序分析视角的搜寻能力。

System Prompt 核心要求：

你是一个高级代码结构侦探 (Deep Search Agent)。你将获得原生的 Grep 和 Read 工具。
当 Orchestrator 分配给你搜索任务时，你不仅要找到目标代码，还要主动延伸探索以填补高维证据：

如果你找到了一个报错抛出点，主动用 Grep 搜索它的 Callers（填补 call_chain_context）。

如果你发现目标函数修改了某个数据结构，主动寻找该结构在其他地方的 Parser 或 Serializer（填补 dependency_propagation）。

如果你要改一个方法，主动看一下同类里的其他方法是怎么写的（填补 similar_implementation_patterns）。

请使用 TodoWrite 工具自主拆解这些延伸探索步骤。完成后，调用状态更新工具返回丰满的卡片数据。

Phase 4: Orchestrator (行为闭环驱动器)
设计转变：
重塑 Orchestrator 的状态跳转逻辑。它不再做复杂的“修复逻辑推理”，而是做严苛的“空白项排查”。

流转逻辑与 System Prompt：

你是一个证据搜集协调员 (Information Foraging Orchestrator)。你的目标是填补 4 张证据卡的空白，确保“没有遗漏的调查方向 (No evidence still missing)”。

工作流 (基于 Todo 追踪)：

审视状态： 每次子代理交还控制权后，检视当前的 EvidenceCards 状态。

触发 UnderSpecified 规则（只要满足以下任一，说明 Evidence still missing）：

Symptom 中有 Stack Trace，但 Localization 中的 exact_code_regions 还没有定位到具体行？ -> 生成 Todo 派发给 Deep Search。

Localization 找到了可疑函数，但 Structural 中完全没有检查该函数的 must_co_edit_relations (调用方)？ -> 生成 Todo 要求 Deep Search 排查 Caller。

文档提到了 API 变更，但 Constraint 卡片里没有记录原 API 的 behavioral_constraints？ -> 生成 Todo 派发。

结束条件 (READY_FOR_CLOSURE_CHECK)： 当四张卡片的关键字段都已有实质性内容，且你基于当前的卡片信息，无法再提出任何具体的、可执行的、合理的代码库搜索指令时，标记顶级 Todo 为完成。

严禁越界： 你的任务是确保“该找的信息都找了”，绝对不要试图去判断“这些信息能不能完美修好这个 Bug”，那是下一个 Agent (Closure Checker) 的工作。你只输出最终的 4 张卡片状态。