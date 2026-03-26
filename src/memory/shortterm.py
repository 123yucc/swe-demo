"""Short-term Memory Module.

单 issue 会话状态管理：
- 阶段进度 (phase_state)
- 证据缺口 (evidence_gap)
- 决策审计 (decision_audit)
- 工作缓存 (runtime_cache)

生命周期：issue 完成并最终 patch commit 后触发清理
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field
from enum import Enum


class PhaseStatus(str, Enum):
    """阶段状态。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class SessionState(BaseModel):
    """会话状态。

    记录当前 issue 的运行态信息。
    """
    instance_id: str = Field(..., description="实例 ID")
    phase: str = Field(..., description="当前阶段")
    status: PhaseStatus = Field(default=PhaseStatus.PENDING, description="阶段状态")
    checkpoint: Optional[str] = Field(None, description="检查点信息")
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    phase_history: List[Dict[str, Any]] = Field(default_factory=list, description="阶段历史")
    error_info: Optional[Dict[str, Any]] = Field(None, description="错误信息")
    schema_version: str = Field(default="1.0")

    def advance_phase(self, new_phase: str, checkpoint: Optional[str] = None) -> None:
        """推进到新阶段。"""
        # 记录当前阶段历史
        self.phase_history.append({
            "phase": self.phase,
            "status": self.status.value,
            "ended_at": datetime.utcnow().isoformat()
        })
        self.phase = new_phase
        self.status = PhaseStatus.PENDING
        self.checkpoint = checkpoint
        self.updated_at = datetime.utcnow().isoformat()

    def set_status(self, status: PhaseStatus, error_info: Optional[Dict[str, Any]] = None) -> None:
        """设置状态。"""
        self.status = status
        self.error_info = error_info
        self.updated_at = datetime.utcnow().isoformat()


class GapPriority(str, Enum):
    """缺口优先级。"""
    CRITICAL = "critical"  # 阻塞修复
    HIGH = "high"          # 严重影响
    MEDIUM = "medium"      # 中等影响
    LOW = "low"            # 轻微影响


class EvidenceGap(BaseModel):
    """证据缺口。

    记录需要补充的证据。
    """
    gap_id: str = Field(..., description="缺口 ID")
    card_type: str = Field(..., description="卡片类型 (symptom/localization/constraint/structural)")
    gap_type: str = Field(..., description="缺口类型 (missing/insufficient/ambiguous/conflicting)")
    required_signal: str = Field(..., description="需要的信号/信息")
    priority: GapPriority = Field(default=GapPriority.MEDIUM, description="优先级")
    description: str = Field(default="", description="详细描述")
    suggested_action: Optional[str] = Field(None, description="建议的补充动作")
    source_phase: str = Field(..., description="发现缺口的阶段")
    resolved: bool = Field(default=False, description="是否已解决")
    resolved_at: Optional[str] = Field(None, description="解决时间")
    resolution: Optional[str] = Field(None, description="解决方案")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    schema_version: str = Field(default="1.0")

    def mark_resolved(self, resolution: str) -> None:
        """标记为已解决。"""
        self.resolved = True
        self.resolved_at = datetime.utcnow().isoformat()
        self.resolution = resolution


class DecisionLog(BaseModel):
    """决策日志。

    记录关键决策及其依据。
    """
    decision_id: str = Field(..., description="决策 ID")
    decision: str = Field(..., description="决策内容")
    rationale: str = Field(..., description="决策理由")
    inputs: List[str] = Field(default_factory=list, description="输入依据 (文件路径等)")
    alternatives: List[str] = Field(default_factory=list, description="备选方案")
    result: Optional[str] = Field(None, description="执行结果")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="置信度")
    phase: str = Field(..., description="决策所属阶段")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    schema_version: str = Field(default="1.0")


class RuntimeCache(BaseModel):
    """运行时缓存。

    存储临时计算结果和中间数据。
    """
    instance_id: str = Field(..., description="实例 ID")
    ast_cache: Dict[str, Any] = Field(default_factory=dict, description="AST 解析缓存")
    grep_cache: Dict[str, List[Any]] = Field(default_factory=dict, description="Grep 结果缓存")
    confidence_cache: Dict[str, float] = Field(default_factory=dict, description="置信度计算缓存")
    custom_cache: Dict[str, Any] = Field(default_factory=dict, description="自定义缓存")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    schema_version: str = Field(default="1.0")

    def update_timestamp(self) -> None:
        """更新时间戳。"""
        self.updated_at = datetime.utcnow().isoformat()

    def clear_all(self) -> None:
        """清空所有缓存。"""
        self.ast_cache.clear()
        self.grep_cache.clear()
        self.confidence_cache.clear()
        self.custom_cache.clear()
        self.update_timestamp()


class ShortTermMemory:
    """短期记忆管理器。

    存储布局：workdir/{instance_id}/.memory/shortterm/
    - session_state.json: 会话状态
    - evidence_gaps.json: 证据缺口
    - decision_audit.json: 决策审计
    - runtime_cache.json: 运行时缓存
    """

    def __init__(self, workspace_dir: str, instance_id: str):
        self.instance_id = instance_id
        self.memory_dir = Path(workspace_dir) / instance_id / ".memory" / "shortterm"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.session_state: Optional[SessionState] = None
        self.evidence_gaps: List[EvidenceGap] = []
        self.decision_logs: List[DecisionLog] = []
        self.runtime_cache: Optional[RuntimeCache] = None

        self._load_all()

    def _load_all(self) -> None:
        """加载所有短期记忆。"""
        self._load_session_state()
        self._load_evidence_gaps()
        self._load_decision_logs()
        self._load_runtime_cache()

    def _load_session_state(self) -> None:
        """加载会话状态。"""
        path = self.memory_dir / "session_state.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.session_state = SessionState(**data)
        else:
            self.session_state = SessionState(
                instance_id=self.instance_id,
                phase="init"
            )

    def _load_evidence_gaps(self) -> None:
        """加载证据缺口。"""
        path = self.memory_dir / "evidence_gaps.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.evidence_gaps = [EvidenceGap(**item) for item in data.get("gaps", [])]

    def _load_decision_logs(self) -> None:
        """加载决策日志。"""
        path = self.memory_dir / "decision_audit.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.decision_logs = [DecisionLog(**item) for item in data.get("decisions", [])]

    def _load_runtime_cache(self) -> None:
        """加载运行时缓存。"""
        path = self.memory_dir / "runtime_cache.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.runtime_cache = RuntimeCache(**data)
        else:
            self.runtime_cache = RuntimeCache(instance_id=self.instance_id)

    def save_all(self) -> None:
        """保存所有短期记忆。"""
        self._save_session_state()
        self._save_evidence_gaps()
        self._save_decision_logs()
        self._save_runtime_cache()

    def _save_session_state(self) -> None:
        """保存会话状态。"""
        if self.session_state:
            path = self.memory_dir / "session_state.json"
            path.write_text(
                json.dumps(self.session_state.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    def _save_evidence_gaps(self) -> None:
        """保存证据缺口。"""
        path = self.memory_dir / "evidence_gaps.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "gaps": [gap.model_dump() for gap in self.evidence_gaps]
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_decision_logs(self) -> None:
        """保存决策日志。"""
        path = self.memory_dir / "decision_audit.json"
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "decisions": [log.model_dump() for log in self.decision_logs]
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_runtime_cache(self) -> None:
        """保存运行时缓存。"""
        if self.runtime_cache:
            path = self.memory_dir / "runtime_cache.json"
            path.write_text(
                json.dumps(self.runtime_cache.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    # === 会话状态管理 ===

    def init_session(self, initial_phase: str = "phase1") -> None:
        """初始化会话。"""
        if not self.session_state:
            self.session_state = SessionState(
                instance_id=self.instance_id,
                phase=initial_phase
            )
        else:
            self.session_state.phase = initial_phase
            self.session_state.status = PhaseStatus.IN_PROGRESS
            self.session_state.updated_at = datetime.utcnow().isoformat()

    def advance_phase(self, new_phase: str, checkpoint: Optional[str] = None) -> None:
        """推进到新阶段。"""
        if self.session_state:
            self.session_state.advance_phase(new_phase, checkpoint)
        self.save_all()

    def set_phase_status(self, status: PhaseStatus, error_info: Optional[Dict[str, Any]] = None) -> None:
        """设置当前阶段状态。"""
        if self.session_state:
            self.session_state.set_status(status, error_info)
        self.save_all()

    # === 证据缺口管理 ===

    def add_evidence_gap(
        self,
        card_type: str,
        gap_type: str,
        required_signal: str,
        priority: GapPriority = GapPriority.MEDIUM,
        description: str = "",
        suggested_action: Optional[str] = None,
        source_phase: str = ""
    ) -> EvidenceGap:
        """添加证据缺口。"""
        gap_id = f"gap_{len(self.evidence_gaps)}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        gap = EvidenceGap(
            gap_id=gap_id,
            card_type=card_type,
            gap_type=gap_type,
            required_signal=required_signal,
            priority=priority,
            description=description,
            suggested_action=suggested_action,
            source_phase=source_phase
        )
        self.evidence_gaps.append(gap)
        self._save_evidence_gaps()
        return gap

    def resolve_gap(self, gap_id: str, resolution: str) -> bool:
        """解决证据缺口。"""
        for gap in self.evidence_gaps:
            if gap.gap_id == gap_id:
                gap.mark_resolved(resolution)
                self._save_evidence_gaps()
                return True
        return False

    def get_unresolved_gaps(self, card_type: Optional[str] = None) -> List[EvidenceGap]:
        """获取未解决的缺口。"""
        gaps = [g for g in self.evidence_gaps if not g.resolved]
        if card_type:
            gaps = [g for g in gaps if g.card_type == card_type]
        # 按优先级排序
        priority_order = {
            GapPriority.CRITICAL: 0,
            GapPriority.HIGH: 1,
            GapPriority.MEDIUM: 2,
            GapPriority.LOW: 3
        }
        gaps.sort(key=lambda g: priority_order.get(g.priority, 4))
        return gaps

    # === 决策日志管理 ===

    def log_decision(
        self,
        decision: str,
        rationale: str,
        phase: str,
        inputs: Optional[List[str]] = None,
        alternatives: Optional[List[str]] = None,
        confidence: float = 0.5
    ) -> DecisionLog:
        """记录决策。"""
        decision_id = f"dec_{len(self.decision_logs)}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        log = DecisionLog(
            decision_id=decision_id,
            decision=decision,
            rationale=rationale,
            inputs=inputs or [],
            alternatives=alternatives or [],
            confidence=confidence,
            phase=phase
        )
        self.decision_logs.append(log)
        self._save_decision_logs()
        return log

    def update_decision_result(self, decision_id: str, result: str) -> bool:
        """更新决策结果。"""
        for log in self.decision_logs:
            if log.decision_id == decision_id:
                log.result = result
                self._save_decision_logs()
                return True
        return False

    def get_decisions_by_phase(self, phase: str) -> List[DecisionLog]:
        """获取指定阶段的决策。"""
        return [log for log in self.decision_logs if log.phase == phase]

    # === 缓存管理 ===

    def cache_ast_result(self, file_path: str, ast_data: Any) -> None:
        """缓存 AST 解析结果。"""
        if self.runtime_cache:
            self.runtime_cache.ast_cache[file_path] = ast_data
            self.runtime_cache.update_timestamp()

    def get_cached_ast(self, file_path: str) -> Optional[Any]:
        """获取缓存的 AST。"""
        if self.runtime_cache:
            return self.runtime_cache.ast_cache.get(file_path)
        return None

    def cache_grep_result(self, pattern: str, results: List[Any]) -> None:
        """缓存 Grep 结果。"""
        if self.runtime_cache:
            self.runtime_cache.grep_cache[pattern] = results
            self.runtime_cache.update_timestamp()

    def get_cached_grep(self, pattern: str) -> Optional[List[Any]]:
        """获取缓存的 Grep 结果。"""
        if self.runtime_cache:
            return self.runtime_cache.grep_cache.get(pattern)
        return None

    def cache_confidence(self, key: str, value: float) -> None:
        """缓存置信度计算结果。"""
        if self.runtime_cache:
            self.runtime_cache.confidence_cache[key] = value
            self.runtime_cache.update_timestamp()

    def get_cached_confidence(self, key: str) -> Optional[float]:
        """获取缓存的置信度。"""
        if self.runtime_cache:
            return self.runtime_cache.confidence_cache.get(key)
        return None

    # === 生命周期管理 ===

    def clear_all(self) -> None:
        """清空所有短期记忆（在 patch commit 后调用）。"""
        self.session_state = None
        self.evidence_gaps.clear()
        self.decision_logs.clear()
        if self.runtime_cache:
            self.runtime_cache.clear_all()

        # 删除文件
        for file in self.memory_dir.glob("*.json"):
            file.unlink()

    def export_audit_summary(self) -> Dict[str, Any]:
        """导出审计摘要（清理前可保留）。"""
        return {
            "instance_id": self.instance_id,
            "exported_at": datetime.utcnow().isoformat(),
            "session_summary": self.session_state.model_dump() if self.session_state else None,
            "gaps_resolved": len([g for g in self.evidence_gaps if g.resolved]),
            "gaps_unresolved": len([g for g in self.evidence_gaps if not g.resolved]),
            "decisions_count": len(self.decision_logs),
            "phase_history": self.session_state.phase_history if self.session_state else []
        }

    def get_statistics(self) -> Dict[str, Any]:
        """获取短期记忆统计。"""
        return {
            "instance_id": self.instance_id,
            "current_phase": self.session_state.phase if self.session_state else None,
            "current_status": self.session_state.status.value if self.session_state else None,
            "total_gaps": len(self.evidence_gaps),
            "unresolved_gaps": len([g for g in self.evidence_gaps if not g.resolved]),
            "decisions_count": len(self.decision_logs),
            "cache_size": {
                "ast": len(self.runtime_cache.ast_cache) if self.runtime_cache else 0,
                "grep": len(self.runtime_cache.grep_cache) if self.runtime_cache else 0,
                "confidence": len(self.runtime_cache.confidence_cache) if self.runtime_cache else 0
            }
        }
