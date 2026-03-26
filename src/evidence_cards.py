"""Evidence Card 数据模型定义。

定义四类核心 evidence 的数据结构：
1. Symptom Evidence: 问题现象和修复目标
2. Localization Evidence: 候选修复位置
3. Constraint Evidence: 修复约束和边界
4. Structural Evidence: 依赖关系和协同修改需求
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field
from enum import Enum


class SufficiencyStatus(str, Enum):
    """证据充分性状态。"""
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class EvidenceSource(BaseModel):
    """证据来源溯源。

    记录证据的来源、匹配细节和置信度计算依据。
    """
    source_type: str = Field(..., description="来源类型 (artifact/repo/test/ast/llm)")
    source_path: str = Field(..., description="来源文件路径")
    matching_detail: Dict[str, Any] = Field(default_factory=dict, description="匹配细节")
    confidence_contribution: float = Field(default=1.0, description="置信度贡献 (0-1)")


class ObservedFailure(BaseModel):
    """观察到的失败现象"""
    description: str = Field(..., description="错误现象的详细描述")
    trigger_condition: Optional[str] = Field(None, description="触发条件")
    exception_type: Optional[str] = Field(None, description="异常类型")
    stack_trace_summary: Optional[str] = Field(None, description="堆栈跟踪摘要")
    error_message: Optional[str] = Field(None, description="错误信息")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")


class ExpectedBehavior(BaseModel):
    """预期行为。"""
    description: str = Field(..., description="修复后的预期行为")
    grounded_in: str = Field(..., description="依据来源 (requirements/tests/docs)")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")


class EntityReference(BaseModel):
    """代码实体引用。"""
    name: str = Field(..., description="实体名称")
    type: str = Field(..., description="实体类型 (function/class/module/route/config)")
    file_path: Optional[str] = Field(None, description="所在文件路径")
    line_number: Optional[int] = Field(None, description="所在行号")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")
    computed_confidence: float = Field(default=0.5, description="计算后的置信度 (0-1)")


class SymptomCard(BaseModel):
    """症状证据卡。

    回答：现在坏在哪里，修好以后应该表现成什么样。
    """
    version: int = Field(default=1, description="Card 版本号")
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_by: str = Field(..., description="更新的 agent 名称")

    # 核心内容 - LLM结构化提取
    observed_failure: ObservedFailure = Field(..., description="观察到的失败现象")
    expected_behavior: ExpectedBehavior = Field(..., description="预期行为")

    # 初步定位线索
    mentioned_entities: List[EntityReference] = Field(default_factory=list, description="提到的代码实体")
    hinted_scope: Optional[str] = Field(None, description="提示的作用域")

    # 充分性评估 - Phase 1只要求"初步提取/存在性"
    sufficiency_status: SufficiencyStatus = Field(default=SufficiencyStatus.UNKNOWN)
    sufficiency_notes: str = Field(default="", description="充分性评估说明")

    # 版本记录
    evidence_sources: List[str] = Field(default_factory=list, description="使用的artifacts")
    missing_artifacts: List[str] = Field(default_factory=list, description="缺失的artifacts")


class CandidateLocation(BaseModel):
    """候选修复位置。"""
    file_path: str = Field(..., description="文件路径")
    symbol_name: Optional[str] = Field(None, description="符号名称 (函数/类)")
    symbol_type: Optional[str] = Field(None, description="符号类型")
    region_start: Optional[int] = Field(None, description="起始行号")
    region_end: Optional[int] = Field(None, description="结束行号")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")
    computed_confidence: float = Field(default=0.5, description="计算后的置信度 (0-1)，基于来源权重+匹配精确度+LLM评分")


class LocalizationCard(BaseModel):
    """定位证据卡。

    回答：应该改哪里。
    """
    version: int = Field(default=1)
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_by: str = Field(...)

    # 候选位置 - 基于文本grep和AST分析
    candidate_locations: List[CandidateLocation] = Field(default_factory=list)

    # 映射关系 - 自动生成
    test_to_code_mappings: Dict[str, str] = Field(default_factory=dict, description="测试到代码的映射")
    interface_to_code_mappings: Dict[str, str] = Field(default_factory=dict, description="接口到代码的映射 (route->文件/函数)")

    # 充分性评估
    sufficiency_status: SufficiencyStatus = Field(default=SufficiencyStatus.UNKNOWN)
    sufficiency_notes: str = Field(default="")


class Constraint(BaseModel):
    """约束定义。"""
    type: str = Field(..., description="约束类型 (requirement/interface/api/type/assertion/edge_case/compatibility)")
    description: str = Field(..., description="约束描述")
    source: str = Field(..., description="来源")
    severity: str = Field(default="must", description="严重程度 (must/should/can)")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")


class ConstraintCard(BaseModel):
    """约束证据卡。

    回答：什么修改才算正确，什么不能改坏。
    """
    version: int = Field(default=1)
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_by: str = Field(...)

    # Constraint Frame 组件
    must_do: List[str] = Field(default_factory=list, description="必须做的约束")
    must_not_break: List[str] = Field(default_factory=list, description="不能破坏的约束")
    allowed_behavior: List[str] = Field(default_factory=list, description="允许的行为")
    forbidden_behavior: List[str] = Field(default_factory=list, description="禁止的行为")
    compatibility_expectations: List[str] = Field(default_factory=list, description="兼容性期望")
    edge_case_obligations: List[str] = Field(default_factory=list, description="边界情况义务")

    # 约束列表
    constraints: List[Constraint] = Field(default_factory=list)

    # API 约束
    api_signatures: Dict[str, str] = Field(default_factory=dict, description="API 签名约束")
    type_constraints: Dict[str, str] = Field(default_factory=dict, description="类型约束")

    # 兼容性要求
    backward_compatibility: bool = Field(default=True, description="是否需要向后兼容")
    compatibility_notes: str = Field(default="", description="兼容性说明")

    # 充分性
    sufficiency_status: SufficiencyStatus = Field(default=SufficiencyStatus.UNKNOWN)
    sufficiency_notes: str = Field(default="")


class DependencyEdge(BaseModel):
    """依赖边。"""
    from_entity: str = Field(..., description="起点实体")
    to_entity: str = Field(..., description="终点实体")
    edge_type: str = Field(..., description="边类型 (caller-callee/import/wrapper/adapter/config-code-test)")
    strength: str = Field(default="strong", description="依赖强度 (strong/medium/weak)")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")


class CoEditGroup(BaseModel):
    """协同编辑组。"""
    group_id: str = Field(..., description="组 ID")
    entities: List[str] = Field(..., description="必须一起修改的实体")
    reason: str = Field(..., description="原因")
    evidence_source: List[EvidenceSource] = Field(default_factory=list, description="证据来源")


class StructuralCard(BaseModel):
    """结构证据卡。

    回答：哪些位置必须一起改，修改之间是什么依赖关系。
    """
    version: int = Field(default=1)
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_by: str = Field(...)

    # 依赖关系 - 基于AST分析
    dependency_edges: List[DependencyEdge] = Field(default_factory=list)

    # 协同修改 - 识别自同一路径的handler+helper等
    co_edit_groups: List[CoEditGroup] = Field(default_factory=list)

    # 传播风险
    propagation_risks: List[str] = Field(default_factory=list, description="传播风险描述")

    # 充分性
    sufficiency_status: SufficiencyStatus = Field(default=SufficiencyStatus.UNKNOWN)
    sufficiency_notes: str = Field(default="")


# 用于版本控制的卡片历史
class CardVersion(BaseModel):
    """卡片版本记录。"""
    version: int
    created_at: str
    created_by: str
    change_summary: str
    card_data: Dict[str, Any]
