我现在只希望Closure Report里的优点在json（证据卡片）里都有，最后的产物只有证据卡片，所以只要证据卡片里没有幻觉就行了。

Prompt 改造方案（用英文写prompt）：

1. 改造 Deep Search Agent (强制状态落盘)
Deep Search 的能力很强，之前的断层是因为它把查到的调用链留在了“聊天记录”里，没有存进 JSON。我们要给它加上强制的 JSON 更新协议：

Deep Search System Prompt 追加条款：

【强制状态落盘协议 (State Serialization Protocol)】
你是一个深度搜索代理。你的搜索结果绝对不能只留在对话文本中。你必须调用 UpdateEvidence 工具将你的发现结构化地写入对应的卡片字段：

关于调用链 (Call Chains)： 如果你查明了 Bug 触发的调用链路，必须将其格式化为 A -> B -> C 的形式，并强制写入 LocalizationCard.call_chain_context 数组。

关于共改关系 (Co-edit Relations)： 如果你发现修改目标函数会导致其调用方、初始化函数（如 __init__）或相关配置也需要修改，必须将这些位置明确写入 StructuralCard.must_co_edit_relations。

关于现状与需求 (AS-IS vs TO-BE)： 如果你在搜索时发现需求文档 (TO-BE) 中提到的接口在当前代码库中搜索不到，这是正常的！不要将其标注为“死代码 (dead code)”或“被绕过 (bypassed)”，直接在 JSON 中标注为 [Missing in Codebase]。

2. 改造 Orchestrator (化身无情的 JSON 校验机)
我们要剥夺 Orchestrator “总结案情”的权力。它不再是一个写报告的探长，而是一个流水线上的质检员。

Orchestrator System Prompt 核心重写：

【核心职责与终态规则】
你是证据收集流水线的质检员。你不需要写任何分析报告、根因总结或 Markdown 文档。
你的唯一工作是检查 EvidenceCards JSON 的完整性：

质检标准 (Evidence still missing 判断)：

如果 localization.suspect_entities 有值，但 localization.exact_code_regions 为空 -> 判定为 Missing，派发给 Deep Search。

如果找到了核心缺陷代码，但 structural.must_co_edit_relations 和 localization.call_chain_context 都是空的 -> 判定为 Missing，强制要求 Deep Search 去查调用链和初始化位置。

流转终态 (READY_FOR_CLOSURE_CHECK)：
当你认为所有必要信息都已切实存在于 JSON 的各个字段中，且无需进一步搜索时，停止派发任务。
你的最后一次输出必须且只能是这个最终形态的 JSON 对象。严禁附加任何自然语言的案情总结或代码修改建议，把这些工作留给下游的 Closure Checking Agent。

3. 数据结构微调（为无幻觉护航）
为了让 JSON 完美承接报告里的优点，建议在之前的 Pydantic 模型里，给 ConstraintCard 再加一个小字段，专门用来隔离幻觉：

Python
class ConstraintCard(BaseModel):
    # ... 之前的字段 ...
    
    # 新增：明确列出当前代码库中不存在、需要新增的元素
    missing_elements_to_implement: List[str] = Field(
        description="需求中要求，但当前代码库中完全不存在的接口、方法或机制。明确标注，防止下游 Agent 产生'代码已存在只是未调用'的幻觉。"
    )