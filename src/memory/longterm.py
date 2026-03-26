"""Long-term Memory Module.

跨 issue 复用知识存储：
- 修复模式库 (patterns)
- 工具/检索策略 (retrieval)
- 置信度权重学习 (weights)
- 失败案例反模式 (antipatterns)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    """长期记忆条目。

    用于存储可复用的修复模式、策略等。
    """
    id: str = Field(..., description="唯一标识符")
    topic: str = Field(..., description="主题分类 (pattern/retrieval/weight/antipattern)")
    signal: str = Field(..., description="触发信号/模式特征")
    action: str = Field(..., description="建议的修复动作")
    outcome: str = Field(..., description="结果描述 (success/failure/partial)")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="置信度")
    evidence_refs: List[str] = Field(default_factory=list, description="关联的证据文件")
    last_used_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    decay_score: float = Field(default=1.0, ge=0.0, le=1.0, description="衰减分数")
    use_count: int = Field(default=0, description="使用次数")
    success_count: int = Field(default=0, description="成功次数")
    tags: List[str] = Field(default_factory=list, description="标签，用于检索")
    issue_type: Optional[str] = Field(None, description="问题类型")
    module_scope: Optional[str] = Field(None, description="模块范围")
    schema_version: str = Field(default="1.0", description="Schema 版本")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def update_usage(self, success: bool) -> None:
        """更新使用统计。"""
        self.use_count += 1
        if success:
            self.success_count += 1
        self.last_used_at = datetime.utcnow().isoformat()
        # 更新置信度
        if self.use_count > 0:
            self.confidence = self.success_count / self.use_count

    def apply_decay(self, decay_rate: float = 0.95) -> None:
        """应用时间衰减。"""
        self.decay_score *= decay_rate
        # 如果太久没使用，进一步衰减
        last_used = datetime.fromisoformat(self.last_used_at)
        days_since_use = (datetime.utcnow() - last_used).days
        if days_since_use > 30:
            self.decay_score *= 0.9 ** (days_since_use // 30)


class WeightProfile(BaseModel):
    """置信度权重配置。

    用于动态调整不同来源证据的权重。
    """
    source_type: str = Field(..., description="来源类型 (artifact/repo/test/ast/llm)")
    match_type: str = Field(..., description="匹配类型 (exact/fuzzy/heuristic)")
    weight: float = Field(default=1.0, ge=0.0, le=2.0, description="权重值")
    sample_size: int = Field(default=0, description="样本数量")
    win_rate: float = Field(default=0.5, ge=0.0, le=1.0, description="成功率")
    last_updated: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    schema_version: str = Field(default="1.0")

    def update_with_result(self, success: bool) -> None:
        """用新结果更新权重配置。"""
        # 使用增量更新
        old_count = self.sample_size
        old_wins = int(self.sample_size * self.win_rate)

        self.sample_size += 1
        if success:
            self.win_rate = (old_wins + 1) / self.sample_size
        else:
            self.win_rate = old_wins / self.sample_size

        # 根据成功率调整权重
        if self.sample_size >= 5:  # 至少5个样本才开始调整
            if self.win_rate > 0.7:
                self.weight = min(2.0, self.weight * 1.1)
            elif self.win_rate < 0.3:
                self.weight = max(0.1, self.weight * 0.9)

        self.last_updated = datetime.utcnow().isoformat()


class AntiPattern(BaseModel):
    """反模式记录。

    记录失败案例和应避免的行为。
    """
    trigger: str = Field(..., description="触发条件")
    bad_action: str = Field(..., description="错误的修复动作")
    impact: str = Field(..., description="影响描述")
    avoidance: str = Field(..., description="应采取的正确做法")
    evidence_refs: List[str] = Field(default_factory=list, description="关联证据")
    occurrence_count: int = Field(default=1, description="出现次数")
    last_occurred: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    tags: List[str] = Field(default_factory=list)
    schema_version: str = Field(default="1.0")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class LongTermMemory:
    """长期记忆管理器。

    存储布局：workdir/.memory/longterm/
    - patterns.json: 修复模式
    - retrieval.json: 检索策略
    - weights.json: 权重配置
    - antipatterns.json: 反模式
    """

    def __init__(self, workspace_dir: str):
        self.memory_dir = Path(workspace_dir) / ".memory" / "longterm"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.patterns: List[MemoryItem] = []
        self.retrieval: List[MemoryItem] = []
        self.weights: Dict[str, WeightProfile] = {}
        self.antipatterns: List[AntiPattern] = []

        self._load_all()

    def _load_all(self) -> None:
        """加载所有长期记忆。"""
        self._load_patterns()
        self._load_retrieval()
        self._load_weights()
        self._load_antipatterns()

    def _load_patterns(self) -> None:
        """加载修复模式。"""
        path = self.memory_dir / "patterns.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.patterns = [MemoryItem(**item) for item in data.get("items", [])]

    def _load_retrieval(self) -> None:
        """加载检索策略。"""
        path = self.memory_dir / "retrieval.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.retrieval = [MemoryItem(**item) for item in data.get("items", [])]

    def _load_weights(self) -> None:
        """加载权重配置。"""
        path = self.memory_dir / "weights.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.weights = {k: WeightProfile(**v) for k, v in data.get("profiles", {}).items()}

    def _load_antipatterns(self) -> None:
        """加载反模式。"""
        path = self.memory_dir / "antipatterns.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.antipatterns = [AntiPattern(**item) for item in data.get("items", [])]

    def save_all(self) -> None:
        """保存所有长期记忆。"""
        self._save_patterns()
        self._save_retrieval()
        self._save_weights()
        self._save_antipatterns()

    def _save_patterns(self) -> None:
        """保存修复模式。"""
        path = self.memory_dir / "patterns.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "items": [item.model_dump() for item in self.patterns]
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_retrieval(self) -> None:
        """保存检索策略。"""
        path = self.memory_dir / "retrieval.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "items": [item.model_dump() for item in self.retrieval]
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_weights(self) -> None:
        """保存权重配置。"""
        path = self.memory_dir / "weights.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "profiles": {k: v.model_dump() for k, v in self.weights.items()}
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_antipatterns(self) -> None:
        """保存反模式。"""
        path = self.memory_dir / "antipatterns.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "items": [item.model_dump() for item in self.antipatterns]
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # === 模式管理 ===

    def add_pattern(self, pattern: MemoryItem) -> None:
        """添加修复模式。"""
        # 检查是否已存在相似模式
        for existing in self.patterns:
            if existing.signal == pattern.signal and existing.action == pattern.action:
                existing.update_usage(pattern.outcome == "success")
                return
        self.patterns.append(pattern)

    def find_matching_patterns(self, signal: str, tags: Optional[List[str]] = None) -> List[MemoryItem]:
        """查找匹配的修复模式。"""
        results = []
        for pattern in self.patterns:
            # 信号匹配（简单包含检查，可以后续改进为语义匹配）
            if signal.lower() in pattern.signal.lower() or pattern.signal.lower() in signal.lower():
                # 标签过滤
                if tags and not any(t in pattern.tags for t in tags):
                    continue
                # 应用衰减分数
                pattern.apply_decay()
                if pattern.decay_score > 0.3:  # 过滤衰减过多的模式
                    results.append(pattern)
        # 按置信度 * 衰减分数排序
        results.sort(key=lambda x: x.confidence * x.decay_score, reverse=True)
        return results

    # === 权重管理 ===

    def get_weight(self, source_type: str, match_type: str) -> float:
        """获取指定来源和匹配类型的权重。"""
        key = f"{source_type}:{match_type}"
        if key in self.weights:
            return self.weights[key].weight
        # 默认权重
        default_weights = {
            "artifact:exact": 1.0,
            "artifact:fuzzy": 0.7,
            "repo:exact": 0.9,
            "repo:fuzzy": 0.6,
            "test:exact": 1.0,
            "test:fuzzy": 0.8,
            "ast:exact": 0.9,
            "ast:fuzzy": 0.7,
            "llm:exact": 0.8,
            "llm:fuzzy": 0.5,
        }
        return default_weights.get(key, 0.5)

    def update_weight(self, source_type: str, match_type: str, success: bool) -> None:
        """更新权重配置。"""
        key = f"{source_type}:{match_type}"
        if key not in self.weights:
            self.weights[key] = WeightProfile(
                source_type=source_type,
                match_type=match_type
            )
        self.weights[key].update_with_result(success)

    # === 反模式管理 ===

    def add_antipattern(self, antipattern: AntiPattern) -> None:
        """添加反模式。"""
        # 检查是否已存在
        for existing in self.antipatterns:
            if existing.trigger == antipattern.trigger and existing.bad_action == antipattern.bad_action:
                existing.occurrence_count += 1
                existing.last_occurred = datetime.utcnow().isoformat()
                return
        self.antipatterns.append(antipattern)

    def check_antipatterns(self, trigger: str, proposed_action: str) -> Optional[AntiPattern]:
        """检查是否存在反模式匹配。"""
        for ap in self.antipatterns:
            if trigger.lower() in ap.trigger.lower():
                if ap.bad_action.lower() in proposed_action.lower():
                    return ap
        return None

    # === 检索策略管理 ===

    def add_retrieval_strategy(self, strategy: MemoryItem) -> None:
        """添加检索策略。"""
        for existing in self.retrieval:
            if existing.signal == strategy.signal:
                existing.update_usage(strategy.outcome == "success")
                return
        self.retrieval.append(strategy)

    def get_retrieval_strategies(self, issue_type: Optional[str] = None) -> List[MemoryItem]:
        """获取检索策略。"""
        results = []
        for strategy in self.retrieval:
            if issue_type and strategy.issue_type != issue_type:
                continue
            strategy.apply_decay()
            if strategy.decay_score > 0.3:
                results.append(strategy)
        results.sort(key=lambda x: x.confidence * x.decay_score, reverse=True)
        return results

    # === 生命周期管理 ===

    def apply_global_decay(self) -> None:
        """应用全局衰减。"""
        for pattern in self.patterns:
            pattern.apply_decay()
        for strategy in self.retrieval:
            strategy.apply_decay()

        # 移除衰减过多的条目
        self.patterns = [p for p in self.patterns if p.decay_score > 0.1]
        self.retrieval = [s for s in self.retrieval if s.decay_score > 0.1]

    def cleanup_expired(self, max_age_days: int = 180) -> int:
        """清理过期记忆。"""
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        removed = 0

        original_count = len(self.patterns)
        self.patterns = [
            p for p in self.patterns
            if datetime.fromisoformat(p.last_used_at) > cutoff or p.success_count >= 2
        ]
        removed += original_count - len(self.patterns)

        original_count = len(self.retrieval)
        self.retrieval = [
            s for s in self.retrieval
            if datetime.fromisoformat(s.last_used_at) > cutoff or s.success_count >= 2
        ]
        removed += original_count - len(self.retrieval)

        return removed

    def get_statistics(self) -> Dict[str, Any]:
        """获取记忆统计信息。"""
        return {
            "patterns_count": len(self.patterns),
            "retrieval_count": len(self.retrieval),
            "weights_count": len(self.weights),
            "antipatterns_count": len(self.antipatterns),
            "avg_pattern_confidence": sum(p.confidence for p in self.patterns) / len(self.patterns) if self.patterns else 0,
            "total_pattern_uses": sum(p.use_count for p in self.patterns),
        }
