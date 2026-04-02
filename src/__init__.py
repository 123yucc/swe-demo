"""Evidence-Closure-Aware Software Engineering Repair Agent."""

__version__ = "0.5.1"
from .evidence_cards import (
    ConstraintCard,
    EvidenceSource,
    LocalizationCard,
    StructuralCard,
    SufficiencyStatus,
    SymptomCard,
)
from .memory import (
    AntiPattern,
    DecisionLog,
    EvidenceGap,
    LongTermMemory,
    MemoryItem,
    MemoryManager,
    SessionState,
    ShortTermMemory,
    WeightProfile,
)
from .contracts.workflow import (
    PhaseStatus,
    TaskSpec,
    TaskStatus,
    TodoItem,
    TodoPriority,
    TodoStatus,
    WorkerSpec,
    WorkflowState,
)
from .run_repair_workflow import run_repair_workflow
from .workers.registry import create_default_registry, create_default_worker_specs

__all__ = [
    "SymptomCard",
    "LocalizationCard",
    "ConstraintCard",
    "StructuralCard",
    "EvidenceSource",
    "SufficiencyStatus",
    "MemoryManager",
    "LongTermMemory",
    "MemoryItem",
    "WeightProfile",
    "AntiPattern",
    "ShortTermMemory",
    "SessionState",
    "EvidenceGap",
    "DecisionLog",
    "run_repair_workflow",
    "WorkflowState",
    "WorkerSpec",
    "TaskSpec",
    "TodoItem",
    "TodoStatus",
    "TodoPriority",
    "PhaseStatus",
    "TaskStatus",
    "create_default_worker_specs",
    "create_default_registry",
]
