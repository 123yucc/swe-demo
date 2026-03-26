"""Memory Manager Module.

统一管理 Long-term 和 Short-term Memory 的协调器。

职责：
- 管理长期记忆的读写入口
- 管理短期记忆的生命周期
- 提供与现有流程的接入点
- 处理记忆的持久化和恢复
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from .longterm import LongTermMemory, MemoryItem, WeightProfile, AntiPattern
from .shortterm import ShortTermMemory, SessionState, EvidenceGap, DecisionLog, PhaseStatus, GapPriority


class MemoryManager:
    """Memory 管理器。

    存储布局：
    workdir/
    ├── .memory/
    │   └── longterm/          # 长期记忆（全局共享）
    │       ├── patterns.json
    │       ├── retrieval.json
    │       ├── weights.json
    │       └── antipatterns.json
    └── {instance_id}/
        └── .memory/
            └── shortterm/     # 短期记忆（单 issue）
                ├── session_state.json
                ├── evidence_gaps.json
                ├── decision_audit.json
                └── runtime_cache.json
    """

    def __init__(self, workspace_dir: str, instance_id: Optional[str] = None):
        self.workspace_dir = Path(workspace_dir)

        # 长期记忆（全局共享）
        self.longterm = LongTermMemory(workspace_dir)

        # 短期记忆（按实例）
        self._instance_id = instance_id
        self._shortterm: Optional[ShortTermMemory] = None

    @property
    def instance_id(self) -> Optional[str]:
        return self._instance_id

    @instance_id.setter
    def instance_id(self, value: str) -> None:
        if self._instance_id != value:
            self._instance_id = value
            self._shortterm = None  # 重置以加载新实例

    @property
    def shortterm(self) -> ShortTermMemory:
        """获取短期记忆实例。"""
        if not self._instance_id:
            raise ValueError("instance_id 必须设置才能访问短期记忆")
        if not self._shortterm:
            self._shortterm = ShortTermMemory(self.workspace_dir, self._instance_id)
        return self._shortterm

    # === 生命周期钩子 ===

    def on_workflow_start(self, instance_id: str) -> None:
        """工作流开始时的初始化。"""
        self.instance_id = instance_id
        self.shortterm.init_session("phase1")

        # 加载长期记忆中的检索策略
        strategies = self.longterm.get_retrieval_strategies()
        if strategies:
            # 缓存到运行时
            self.shortterm.runtime_cache.custom_cache["retrieval_strategies"] = [
                {"signal": s.signal, "action": s.action, "confidence": s.confidence}
                for s in strategies[:5]  # 只取前5个最相关的
            ]
            self.shortterm._save_runtime_cache()

    def on_phase_complete(self, phase: str, success: bool, summary: Optional[Dict[str, Any]] = None) -> None:
        """阶段完成时的处理。"""
        # 更新会话状态
        self.shortterm.set_phase_status(
            PhaseStatus.COMPLETED if success else PhaseStatus.BLOCKED,
            error_info=summary if not success else None
        )

        # 记录决策
        if summary and "decisions" in summary:
            for dec in summary["decisions"]:
                self.shortterm.log_decision(
                    decision=dec.get("decision", ""),
                    rationale=dec.get("rationale", ""),
                    phase=phase,
                    inputs=dec.get("inputs", []),
                    confidence=dec.get("confidence", 0.5)
                )

        # 更新权重学习
        if summary and "evidence_sources" in summary:
            for src in summary["evidence_sources"]:
                self.longterm.update_weight(
                    source_type=src.get("source_type", ""),
                    match_type=src.get("match_type", "exact"),
                    success=success
                )

    def on_gap_detected(
        self,
        card_type: str,
        gap_type: str,
        required_signal: str,
        priority: GapPriority = GapPriority.MEDIUM,
        description: str = "",
        suggested_action: Optional[str] = None,
        source_phase: str = ""
    ) -> EvidenceGap:
        """检测到证据缺口时的处理。"""
        return self.shortterm.add_evidence_gap(
            card_type=card_type,
            gap_type=gap_type,
            required_signal=required_signal,
            priority=priority,
            description=description,
            suggested_action=suggested_action,
            source_phase=source_phase
        )

    def on_gap_resolved(self, gap_id: str, resolution: str) -> bool:
        """证据缺口解决时的处理。"""
        return self.shortterm.resolve_gap(gap_id, resolution)

    def on_patch_commit(self) -> Dict[str, Any]:
        """Patch 提交后的清理。"""
        # 导出审计摘要
        audit_summary = self.shortterm.export_audit_summary()

        # 如果成功，学习有效模式
        if audit_summary.get("session_summary", {}).get("status") == "completed":
            self._learn_successful_patterns()

        # 清理短期记忆
        self.shortterm.clear_all()

        # 保存长期记忆
        self.longterm.save_all()

        return audit_summary

    def _learn_successful_patterns(self) -> None:
        """从成功的修复中学习模式。"""
        decisions = self.shortterm.decision_logs
        for dec in decisions:
            if dec.result == "success" and dec.confidence > 0.7:
                # 创建新的修复模式
                pattern = MemoryItem(
                    id=f"pattern_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{dec.decision_id}",
                    topic="pattern",
                    signal=dec.decision[:100],  # 截断
                    action=dec.rationale[:200],
                    outcome="success",
                    confidence=dec.confidence,
                    evidence_refs=dec.inputs,
                    tags=[dec.phase]
                )
                self.longterm.add_pattern(pattern)

    # === 读取接口 ===

    def get_patterns_for_signal(self, signal: str, tags: Optional[List[str]] = None) -> List[MemoryItem]:
        """获取匹配信号的修复模式。"""
        return self.longterm.find_matching_patterns(signal, tags)

    def get_weight(self, source_type: str, match_type: str) -> float:
        """获取来源权重。"""
        return self.longterm.get_weight(source_type, match_type)

    def check_antipattern(self, trigger: str, proposed_action: str) -> Optional[AntiPattern]:
        """检查是否存在反模式。"""
        return self.longterm.check_antipatterns(trigger, proposed_action)

    def get_unresolved_gaps(self, card_type: Optional[str] = None) -> List[EvidenceGap]:
        """获取未解决的证据缺口。"""
        return self.shortterm.get_unresolved_gaps(card_type)

    def get_cached_ast(self, file_path: str) -> Optional[Any]:
        """获取缓存的 AST。"""
        return self.shortterm.get_cached_ast(file_path)

    def get_cached_grep(self, pattern: str) -> Optional[List[Any]]:
        """获取缓存的 Grep 结果。"""
        return self.shortterm.get_cached_grep(pattern)

    # === 写入接口 ===

    def cache_ast(self, file_path: str, ast_data: Any) -> None:
        """缓存 AST 结果。"""
        self.shortterm.cache_ast_result(file_path, ast_data)

    def cache_grep(self, pattern: str, results: List[Any]) -> None:
        """缓存 Grep 结果。"""
        self.shortterm.cache_grep_result(pattern, results)

    def log_decision(
        self,
        decision: str,
        rationale: str,
        phase: str,
        inputs: Optional[List[str]] = None,
        confidence: float = 0.5
    ) -> DecisionLog:
        """记录决策。"""
        return self.shortterm.log_decision(
            decision=decision,
            rationale=rationale,
            phase=phase,
            inputs=inputs,
            confidence=confidence
        )

    def add_antipattern(
        self,
        trigger: str,
        bad_action: str,
        impact: str,
        avoidance: str,
        evidence_refs: Optional[List[str]] = None,
        tags: Optional[List[str]] = None
    ) -> None:
        """添加反模式。"""
        antipattern = AntiPattern(
            trigger=trigger,
            bad_action=bad_action,
            impact=impact,
            avoidance=avoidance,
            evidence_refs=evidence_refs or [],
            tags=tags or []
        )
        self.longterm.add_antipattern(antipattern)

    # === 持久化 ===

    def save_all(self) -> None:
        """保存所有记忆。"""
        self.longterm.save_all()
        if self._shortterm:
            self.shortterm.save_all()

    def load_state(self) -> bool:
        """从磁盘加载状态。"""
        try:
            self.longterm._load_all()
            if self._instance_id:
                self._shortterm = ShortTermMemory(str(self.workspace_dir), self._instance_id)
            return True
        except Exception:
            return False

    # === 统计 ===

    def get_statistics(self) -> Dict[str, Any]:
        """获取完整的记忆统计。"""
        stats = {
            "longterm": self.longterm.get_statistics(),
            "instance_id": self._instance_id
        }
        if self._shortterm:
            stats["shortterm"] = self.shortterm.get_statistics()
        return stats

    # === 维护 ===

    def run_maintenance(self) -> Dict[str, Any]:
        """运行维护任务。"""
        # 应用衰减
        self.longterm.apply_global_decay()

        # 清理过期记忆
        removed = self.longterm.cleanup_expired()

        # 保存
        self.longterm.save_all()

        return {
            "removed_items": removed,
            "timestamp": datetime.utcnow().isoformat(),
            "stats": self.longterm.get_statistics()
        }

    # === 恢复支持 ===

    def can_resume(self, instance_id: str) -> bool:
        """检查是否可以恢复指定实例。"""
        instance_dir = self.workspace_dir / instance_id / ".memory" / "shortterm"
        session_file = instance_dir / "session_state.json"
        if not session_file.exists():
            return False

        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            state = SessionState(**data)
            return state.status not in [PhaseStatus.COMPLETED]
        except Exception:
            return False

    def get_resume_checkpoint(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """获取恢复检查点。"""
        if not self.can_resume(instance_id):
            return None

        self.instance_id = instance_id
        return {
            "instance_id": instance_id,
            "phase": self.shortterm.session_state.phase,
            "status": self.shortterm.session_state.status.value,
            "checkpoint": self.shortterm.session_state.checkpoint,
            "unresolved_gaps": [
                {"gap_id": g.gap_id, "card_type": g.card_type, "required_signal": g.required_signal}
                for g in self.shortterm.get_unresolved_gaps()
            ]
        }
