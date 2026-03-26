"""Memory Management Module.

提供项目级 Long/Short Memory 管理功能：
- longterm memory: 跨 issue 复用知识（修复模式库、工具/检索策略、置信度权重学习、失败案例反模式）
- shortterm memory: 单 issue 会话导航与恢复（阶段进度、证据缺口、决策审计、工作缓存）

生命周期：
- shortterm: issue 完成并最终 patch commit 后触发清理
- longterm: 保留并定期衰减（基于 last_used_at 与 outcome 成功率）
"""

from .longterm import LongTermMemory, MemoryItem, WeightProfile, AntiPattern
from .shortterm import ShortTermMemory, SessionState, EvidenceGap, DecisionLog
from .manager import MemoryManager

__all__ = [
    # Long-term memory
    "LongTermMemory", "MemoryItem", "WeightProfile", "AntiPattern",
    # Short-term memory
    "ShortTermMemory", "SessionState", "EvidenceGap", "DecisionLog",
    # Manager
    "MemoryManager",
]
