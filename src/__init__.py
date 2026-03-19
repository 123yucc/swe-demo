"""Evidence-Closure-Aware Software Engineering Repair Agent.

改进版本 (Phase 1/2 v2):
- Phase 1: LLM驱动的Artifact解析
- Phase 2: 动态证据提取 (文本+AST分析)
- 动态置信度计算
- 去除硬编码模式
"""

__version__ = "0.2.0"

# 导出主要组件
from .evidence_cards import (
    SymptomCard, LocalizationCard, ConstraintCard, StructuralCard,
    EvidenceSource, SufficiencyStatus
)

from .orchestrator import Orchestrator, run_repair_workflow

# Phase 1: LLM驱动的解析器
from .artifact_parsers_llm import (
    LLMArtifactParser,
    run_phase1_parsing,
    PHASE1_OUTPUT_SCHEMA
)

# Phase 2: 动态证据提取器
from .evidence_extractors_phase2 import (
    DynamicSymptomExtractor,
    DynamicLocalizationExtractor,
    DynamicConstraintExtractor,
    DynamicStructuralExtractor,
    CodebaseNavigator,
    run_phase2_extraction_dynamic
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
    "CodebaseNavigator", "run_phase2_extraction_dynamic",
]
