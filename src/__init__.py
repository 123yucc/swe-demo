"""Evidence-Closure-Aware Software Engineering Repair Agent。

Phase 1: LLM驱动的Artifact解析
Phase 2: 动态证据提取 (文本+AST分析+验证)
Memory: 长期/短期记忆管理
Scheduler: 动态Todo优先级调度
"""

__version__ = "0.5.1"

# Evidence Cards
from .evidence_cards import (
    SymptomCard, LocalizationCard, ConstraintCard, StructuralCard,
    EvidenceSource, SufficiencyStatus
)

# Orchestrator
from .orchestrator import Orchestrator, run_repair_workflow

# Phase 1
from .artifact_parsers_llm import (
    LLMArtifactParser,
    run_phase1_parsing,
    PHASE1_OUTPUT_SCHEMA
)

# Phase 2
from .evidence_extractors_phase2 import (
    DynamicSymptomExtractor,
    DynamicLocalizationExtractor,
    DynamicConstraintExtractor,
    DynamicStructuralExtractor,
    CodebaseNavigator,
    ValidationStatus,
    ValidationResult,
    CodeWindow,
    run_phase2_extraction_dynamic,
    enhance_all_cards,
    extract_symptom_evidence,
    extract_localization_evidence,
    extract_constraint_evidence,
    extract_structural_evidence
)

# Memory
from .memory import (
    MemoryManager,
    LongTermMemory, MemoryItem, WeightProfile, AntiPattern,
    ShortTermMemory, SessionState, EvidenceGap, DecisionLog
)

# Scheduler
from .scheduler import (
    Scheduler, ScheduleResult,
    WorkflowState, WorkerSpec, TaskSpec, TodoItem,
    TodoStatus, TodoPriority, PhaseStatus, WorkerRegistry,
    create_default_registry
)

__all__ = [
    # Evidence Cards
    "SymptomCard", "LocalizationCard", "ConstraintCard", "StructuralCard",
    "EvidenceSource", "SufficiencyStatus",
    # Orchestrator
    "Orchestrator", "run_repair_workflow",
    # Phase 1
    "LLMArtifactParser", "run_phase1_parsing", "PHASE1_OUTPUT_SCHEMA",
    # Phase 2
    "DynamicSymptomExtractor", "DynamicLocalizationExtractor",
    "DynamicConstraintExtractor", "DynamicStructuralExtractor",
    "CodebaseNavigator", "ValidationStatus", "ValidationResult", "CodeWindow",
    "run_phase2_extraction_dynamic", "enhance_all_cards",
    "extract_symptom_evidence", "extract_localization_evidence",
    "extract_constraint_evidence", "extract_structural_evidence",
    # Memory
    "MemoryManager",
    "LongTermMemory", "MemoryItem", "WeightProfile", "AntiPattern",
    "ShortTermMemory", "SessionState", "EvidenceGap", "DecisionLog",
    # Scheduler
    "Scheduler", "ScheduleResult",
    "WorkflowState", "WorkerSpec", "TaskSpec", "TodoItem",
    "TodoStatus", "TodoPriority", "PhaseStatus", "WorkerRegistry",
    "create_default_registry",
]
