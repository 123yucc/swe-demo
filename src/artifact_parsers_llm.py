"""Phase 1: Artifact Parsing - LLM驱动版本。

使用Claude Agent SDK的Agent子代理和结构化输出来解析artifacts。
去除硬编码模式，改为使用LLM进行结构化提取。
"""

import json
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

try:
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition, ResultMessage,
        AssistantMessage, SystemMessage, TaskStartedMessage,
        TaskProgressMessage, TaskNotificationMessage
    )
    CLAUDE_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_SDK_AVAILABLE = False

from .evidence_cards import (
    SymptomCard, LocalizationCard, ConstraintCard, StructuralCard,
    ObservedFailure, ExpectedBehavior, EntityReference, CandidateLocation,
    Constraint, DependencyEdge, CoEditGroup,
    EvidenceSource, SufficiencyStatus
)


# JSON Schema for structured output from LLM
PHASE1_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "symptom": {
            "type": "object",
            "properties": {
                "observed_failure": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "trigger_condition": {"type": "string"},
                        "exception_type": {"type": "string"},
                        "stack_trace_summary": {"type": "string"},
                        "error_message": {"type": "string"}
                    },
                    "required": ["description"]
                },
                "expected_behavior": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "grounded_in": {"type": "string"}
                    },
                    "required": ["description", "grounded_in"]
                },
                "mentioned_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "file_path": {"type": "string"},
                            "line_number": {"type": "number"}
                        },
                        "required": ["name", "type"]
                    }
                },
                "hinted_scope": {"type": "string"}
            },
            "required": ["observed_failure", "expected_behavior"]
        },
        "constraints": {
            "type": "object",
            "properties": {
                "must_do": {"type": "array", "items": {"type": "string"}},
                "must_not_break": {"type": "array", "items": {"type": "string"}},
                "allowed_behavior": {"type": "array", "items": {"type": "string"}},
                "forbidden_behavior": {"type": "array", "items": {"type": "string"}},
                "compatibility_expectations": {"type": "array", "items": {"type": "string"}},
                "edge_case_obligations": {"type": "array", "items": {"type": "string"}},
                "api_signatures": {"type": "object"},
                "type_constraints": {"type": "object"}
            }
        },
        "localization": {
            "type": "object",
            "properties": {
                "initial_anchors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "file_pattern": {"type": "string"}
                        },
                        "required": ["name", "type"]
                    }
                }
            }
        },
        "structural": {
            "type": "object",
            "properties": {
                "api_entry_points": {"type": "array", "items": {"type": "string"}},
                "public_interfaces": {"type": "array", "items": {"type": "string"}}
            }
        },
        "artifacts_used": {"type": "array", "items": {"type": "string"}},
        "artifacts_missing": {"type": "array", "items": {"type": "string"}},
        "sufficiency_notes": {"type": "string"}
    },
    "required": ["symptom", "constraints", "localization", "structural"]
}


class LLMArtifactParser:
    """使用LLM解析artifacts的Phase 1解析器。"""

    def __init__(self, workspace_dir: str, instance_id: str):
        self.workspace_dir = Path(workspace_dir)
        self.instance_id = instance_id
        
        # 检查workspace_dir是否已经包含了instance_id
        if self.workspace_dir.name == instance_id:
            # workspace_dir已经是实例目录，直接使用
            self.instance_dir = self.workspace_dir
        else:
            # workspace_dir是基目录，需要添加instance_id
            self.instance_dir = self.workspace_dir / instance_id
            
        self.artifacts_dir = self.instance_dir / "artifacts"
        self.evidence_dir = self.instance_dir / "evidence"
        self.versions_dir = self.evidence_dir / "card_versions"

    def _load_artifact(self, name: str) -> Optional[str]:
        """加载artifact文件内容。"""
        path = self.artifacts_dir / name
        if path.exists():
            return path.read_text(encoding='utf-8')
        return None

    def _get_available_artifacts(self) -> Dict[str, str]:
        """获取所有可用的artifacts。"""
        artifacts = {}
        expected_files = [
            "problem_statement.md",
            "requirements.md",
            "new_interfaces.md",
            "expected_and current_behavior.md"  # 修正文件名中的空格
        ]

        for filename in expected_files:
            content = self._load_artifact(filename)
            if content:
                artifacts[filename] = content

        return artifacts

    async def parse_with_llm(self) -> Dict[str, Any]:
        """使用LLM解析所有artifacts。

        返回结构化的提取结果，包含所有证据卡的内容。
        """
        if not CLAUDE_SDK_AVAILABLE:
            raise RuntimeError("Claude Agent SDK not available")

        artifacts = self._get_available_artifacts()

        # 构建prompt
        prompt_parts = [
            "You are an Artifact Parser for Phase 1 of an evidence-based repair system.",
            "",
            "Parse the following artifacts and extract structured evidence:",
            ""
        ]

        if not artifacts:
            return await self._fallback_parse()

        for name, content in artifacts.items():
            prompt_parts.append(f"--- {name} ---")
            prompt_parts.append(content)
            prompt_parts.append("")

        prompt_parts.append("Extract the following structured information:")
        prompt_parts.append("")
        prompt_parts.append("1. Symptom Frame:")
        prompt_parts.append("   - observed_failure: description, trigger_condition, exception_type, stack_trace_summary, error_message")
        prompt_parts.append("   - expected_behavior: description, grounded_in")
        prompt_parts.append("   - mentioned_entities: list of {name, type, file_path?, line_number?}")
        prompt_parts.append("   - hinted_scope: scope hint from problem statement")
        prompt_parts.append("")
        prompt_parts.append("2. Constraint Frame:")
        prompt_parts.append("   - must_do, must_not_break, allowed_behavior, forbidden_behavior")
        prompt_parts.append("   - compatibility_expectations, edge_case_obligations")
        prompt_parts.append("   - api_signatures, type_constraints")
        prompt_parts.append("")
        prompt_parts.append("3. Localization:")
        prompt_parts.append("   - initial_anchors: entities/routes mentioned that hint at code locations")
        prompt_parts.append("")
        prompt_parts.append("4. Structural:")
        prompt_parts.append("   - api_entry_points, public_interfaces from interface specs")
        prompt_parts.append("")
        prompt_parts.append("5. Metadata:")
        prompt_parts.append("   - artifacts_used: which files were processed")
        prompt_parts.append("   - artifacts_missing: which expected files were not found")
        prompt_parts.append("   - sufficiency_notes: what's missing for complete analysis")

        prompt = "\n".join(prompt_parts)

        # 使用ClaudeSDKClient而不是query()函数，可能避免signature字段问题
        try:
            async with ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    allowed_tools=["Read", "Glob"],  # 允许读取文件
                    output_format={"type": "json_schema", "schema": PHASE1_OUTPUT_SCHEMA}
                )
            ) as client:
                # 发送查询
                await client.query(prompt)
                
                # 接收响应
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        if message.structured_output:
                            return message.structured_output
                    elif isinstance(message, AssistantMessage):
                        # 检查AssistantMessage中是否有structured_output
                        if hasattr(message, 'structured_output') and message.structured_output:
                            return message.structured_output
                            
        except Exception as e:
            # 捕获SDK内部的消息解析错误
            error_msg = str(e)
            if "signature" in error_msg.lower():
                # 这是已知的SDK问题
                print(f"[WARNING] SDK消息解析错误(ClaudeSDKClient): {error_msg}")
                print("[INFO] 回退到基础提取...")
                return await self._fallback_parse()
            else:
                raise

        raise RuntimeError("Failed to get structured output from LLM")

    async def _fallback_parse(self) -> Dict[str, Any]:
        """当SDK发生错误时的备用解析方法。"""
        artifacts = self._get_available_artifacts()
        
        # 生成基意的提取结果
        return {
            "symptom": {
                "observed_failure": {
                    "description": "Unknown failure (fallback mode)",
                    "trigger_condition": None,
                    "exception_type": None,
                    "stack_trace_summary": None,
                    "error_message": None
                },
                "expected_behavior": {
                    "description": "Fix the issue",
                    "grounded_in": "problem_statement"
                },
                "mentioned_entities": [],
                "hinted_scope": None
            },
            "constraints": {
                "must_do": [],
                "must_not_break": [],
                "allowed_behavior": [],
                "forbidden_behavior": [],
                "compatibility_expectations": [],
                "edge_case_obligations": [],
                "api_signatures": {},
                "type_constraints": {}
            },
            "localization": {
                "initial_anchors": []
            },
            "structural": {
                "api_entry_points": [],
                "public_interfaces": []
            },
            "artifacts_used": list(artifacts.keys()),
            "artifacts_missing": [
                "problem_statement.md",
                "requirements.md",
                "interface.md",
                "new_interfaces.md",
                "expected_and_current_behavior.md"
            ] if not artifacts else [],
            "sufficiency_notes": "Fallback parse due to SDK error. Limited extraction available."
        }

    def _compute_confidence(self, source_type: str, match_quality: str) -> float:
        """计算动态置信度。

        基于来源权重和匹配质量计算，不是硬编码。
        """
        # 来源权重
        source_weights = {
            "interface": 1.0,
            "requirement": 0.9,
            "problem_statement": 0.85,
            "expected_behavior": 0.8,
            "heuristic": 0.6
        }

        # 匹配质量
        match_weights = {
            "exact": 1.0,
            "strong": 0.9,
            "fuzzy": 0.7,
            "weak": 0.5
        }

        base = source_weights.get(source_type, 0.5)
        match = match_weights.get(match_quality, 0.5)

        # 最终置信度 = 来源权重 * 匹配质量
        return round(base * match, 2)

    def _flatten_api_signatures(self, api_sigs: Dict[str, Any]) -> Dict[str, str]:
        """将嵌套的api_signatures扁平化为Dict[str, str]。"""
        result = {}
        for key, value in api_sigs.items():
            if isinstance(value, dict):
                # 如果value是dict，转为JSON字符串
                result[key] = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, (list, tuple)):
                result[key] = json.dumps(value, ensure_ascii=False)
            else:
                result[key] = str(value)
        return result

    def _flatten_dict_to_strings(self, data: Dict[str, Any]) -> Dict[str, str]:
        """将任何dict值转为字符串."""
        result = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                result[key] = json.dumps(value, ensure_ascii=False)
            else:
                result[key] = str(value)
        return result

    def _build_evidence_cards(self, parsed_data: Dict[str, Any]) -> Dict[str, Any]:
        """从解析数据构建证据卡对象。"""
        now = datetime.utcnow().isoformat()
        updated_by = "phase1_parser"

        # 构建SymptomCard
        symptom_data = parsed_data.get("symptom", {})
        observed = symptom_data.get("observed_failure", {})
        expected = symptom_data.get("expected_behavior", {})

        # 为mentioned_entities添加evidence_source和computed_confidence
        entities = []
        for ent in symptom_data.get("mentioned_entities", []):
            source_type = "problem_statement"
            match_quality = "exact" if ent.get("file_path") else "fuzzy"
            confidence = self._compute_confidence(source_type, match_quality)

            entities.append(EntityReference(
                name=ent["name"],
                type=ent["type"],
                file_path=ent.get("file_path"),
                line_number=ent.get("line_number"),
                evidence_source=[EvidenceSource(
                    source_type=source_type,
                    source_path="problem_statement.md",
                    confidence_contribution=confidence
                )],
                computed_confidence=confidence
            ))

        symptom_card = SymptomCard(
            version=1,
            updated_at=now,
            updated_by=updated_by,
            observed_failure=ObservedFailure(
                description=observed.get("description", "Unknown"),
                trigger_condition=observed.get("trigger_condition"),
                exception_type=observed.get("exception_type"),
                stack_trace_summary=observed.get("stack_trace_summary"),
                error_message=observed.get("error_message"),
                evidence_source=[EvidenceSource(
                    source_type="problem_statement",
                    source_path="problem_statement.md",
                    confidence_contribution=0.85
                )]
            ),
            expected_behavior=ExpectedBehavior(
                description=expected.get("description", "Fix the issue"),
                grounded_in=expected.get("grounded_in", "problem_statement"),
                evidence_source=[EvidenceSource(
                    source_type="expected_behavior",
                    source_path="expected_and_current_behavior.md",
                    confidence_contribution=0.8
                )] if "expected_and_current_behavior.md" in parsed_data.get("artifacts_used", []) else []
            ),
            mentioned_entities=entities,
            hinted_scope=symptom_data.get("hinted_scope"),
            sufficiency_status=self._assess_sufficiency_phase1(parsed_data),
            sufficiency_notes=parsed_data.get("sufficiency_notes", ""),
            evidence_sources=parsed_data.get("artifacts_used", []),
            missing_artifacts=parsed_data.get("artifacts_missing", [])
        )

        # 构建LocalizationCard (v1: initial anchors only)
        localization_data = parsed_data.get("localization", {})
        anchors = localization_data.get("initial_anchors", [])
        candidate_locations = []

        for anchor in anchors:
            confidence = self._compute_confidence("heuristic", "fuzzy")
            candidate_locations.append(CandidateLocation(
                file_path=anchor.get("file_pattern", ""),
                symbol_name=anchor.get("name"),
                symbol_type=anchor.get("type"),
                evidence_source=[EvidenceSource(
                    source_type="heuristic",
                    source_path="problem_statement.md",
                    matching_detail={"anchor": anchor},
                    confidence_contribution=confidence
                )],
                computed_confidence=confidence
            ))

        localization_card = LocalizationCard(
            version=1,
            updated_at=now,
            updated_by=updated_by,
            candidate_locations=candidate_locations,
            test_to_code_mappings={},
            interface_to_code_mappings={},
            sufficiency_status=SufficiencyStatus.PARTIAL,  # Phase 1: only anchors
            sufficiency_notes="Phase 1: Initial anchors only. Need Phase 2 for precise locations."
        )

        # 构建ConstraintCard (v1)
        constraints_data = parsed_data.get("constraints", {})
        constraints = []

        # Convert constraint lists to Constraint objects
        for item in constraints_data.get("must_do", []):
            constraints.append(Constraint(
                type="must",
                description=item,
                source="requirements",
                severity="must"
            ))

        for item in constraints_data.get("must_not_break", []):
            constraints.append(Constraint(
                type="must_not",
                description=item,
                source="requirements",
                severity="must"
            ))

        constraint_card = ConstraintCard(
            version=1,
            updated_at=now,
            updated_by=updated_by,
            must_do=constraints_data.get("must_do", []),
            must_not_break=constraints_data.get("must_not_break", []),
            allowed_behavior=constraints_data.get("allowed_behavior", []),
            forbidden_behavior=constraints_data.get("forbidden_behavior", []),
            compatibility_expectations=constraints_data.get("compatibility_expectations", []),
            edge_case_obligations=constraints_data.get("edge_case_obligations", []),
            constraints=constraints,
            # 处理api_signatures格式：如果是嵌套dict，只提取keys
            api_signatures=self._flatten_api_signatures(constraints_data.get("api_signatures", {})),
            type_constraints=self._flatten_dict_to_strings(constraints_data.get("type_constraints", {})),
            backward_compatibility=True,  # Default assumption
            sufficiency_status=SufficiencyStatus.PARTIAL,
            sufficiency_notes="Phase 1: Constraints from artifacts only. Need Phase 2 for code-level constraints."
        )

        # 构建StructuralCard (v1: API signatures only)
        structural_data = parsed_data.get("structural", {})
        structural_card = StructuralCard(
            version=1,
            updated_at=now,
            updated_by=updated_by,
            dependency_edges=[],
            co_edit_groups=[],
            propagation_risks=[],
            sufficiency_status=SufficiencyStatus.PARTIAL,
            sufficiency_notes="Phase 1: API entry points only. Need Phase 2 for dependency analysis."
        )

        return {
            "symptom": symptom_card,
            "localization": localization_card,
            "constraint": constraint_card,
            "structural": structural_card
        }

    def _assess_sufficiency_phase1(self, parsed_data: Dict[str, Any]) -> SufficiencyStatus:
        """Phase 1充分性评估：只检查基本存在性。"""
        used = parsed_data.get("artifacts_used", [])
        missing = parsed_data.get("artifacts_missing", [])

        critical_artifacts = ["problem_statement.md"]
        has_critical = any(a in used for a in critical_artifacts)

        if not has_critical:
            return SufficiencyStatus.INSUFFICIENT

        if missing:
            return SufficiencyStatus.PARTIAL

        return SufficiencyStatus.SUFFICIENT

    def save_cards(self, cards: Dict[str, Any]):
        """保存证据卡到文件系统。"""
        # 确保目录存在
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        (self.versions_dir / "v1").mkdir(exist_ok=True)

        for card_type, card in cards.items():
            # 保存当前版本
            card_path = self.evidence_dir / f"{card_type}_card.json"
            with open(card_path, 'w', encoding='utf-8') as f:
                f.write(card.model_dump_json(indent=2))

            # 保存版本历史
            version_path = self.versions_dir / "v1" / f"{card_type}_card_v1.json"
            with open(version_path, 'w', encoding='utf-8') as f:
                f.write(card.model_dump_json(indent=2))

    def generate_summary(self, cards: Dict[str, Any], artifacts_used: List[str], artifacts_missing: List[str]) -> Dict[str, Any]:
        """生成Phase 1解析摘要。"""
        return {
            "phase": "Phase 1: Artifact Parsing",
            "timestamp": datetime.utcnow().isoformat(),
            "instance_id": self.instance_id,
            "artifacts_used": artifacts_used,
            "artifacts_missing": artifacts_missing,
            "evidence_cards": {
                "symptom": {
                    "sufficiency_status": cards["symptom"].sufficiency_status.value,
                    "sufficiency_notes": cards["symptom"].sufficiency_notes,
                    "entities_extracted": len(cards["symptom"].mentioned_entities)
                },
                "localization": {
                    "sufficiency_status": cards["localization"].sufficiency_status.value,
                    "sufficiency_notes": cards["localization"].sufficiency_notes,
                    "anchors_extracted": len(cards["localization"].candidate_locations)
                },
                "constraint": {
                    "sufficiency_status": cards["constraint"].sufficiency_status.value,
                    "sufficiency_notes": cards["constraint"].sufficiency_notes,
                    "constraints_extracted": len(cards["constraint"].constraints)
                },
                "structural": {
                    "sufficiency_status": cards["structural"].sufficiency_status.value,
                    "sufficiency_notes": cards["structural"].sufficiency_notes
                }
            },
            "next_phase": "Phase 2: Evidence Extraction"
        }


async def run_phase1_parsing(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    """运行Phase 1 artifact parsing。

    使用LLM提取结构化证据，生成v1版本的证据卡。
    """
    parser = LLMArtifactParser(workspace_dir, instance_id)

    if not CLAUDE_SDK_AVAILABLE:
        # Fallback: 使用基于规则的基础提取
        return await _fallback_phase1(workspace_dir, instance_id)

    # 使用LLM解析
    parsed_data = await parser.parse_with_llm()

    # 构建证据卡
    cards = parser._build_evidence_cards(parsed_data)

    # 保存卡片
    parser.save_cards(cards)

    # 生成摘要
    summary = parser.generate_summary(
        cards,
        parsed_data.get("artifacts_used", []),
        parsed_data.get("artifacts_missing", [])
    )

    # 保存摘要
    summary_path = parser.instance_dir / "evidence" / "phase1_summary.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return {
        "cards": cards,
        "summary": summary
    }


async def _fallback_phase1(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    """当SDK不可用时使用的fallback解析。"""
    from .artifact_parsers import parse_all_artifacts

    instance_dir = Path(workspace_dir) / instance_id
    evidence_dir = instance_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # 使用传统解析
    results = parse_all_artifacts(str(instance_dir))

    # 转换为新格式 (简化版本)
    now = datetime.utcnow().isoformat()
    updated_by = "phase1_parser_fallback"

    # 构建基础cards
    symptom_card = SymptomCard(
        version=1,
        updated_at=now,
        updated_by=updated_by,
        observed_failure=ObservedFailure(
            description="Parsed from problem statement (fallback mode)"
        ),
        expected_behavior=ExpectedBehavior(
            description="Fix the described issue",
            grounded_in="problem_statement"
        ),
        sufficiency_status=SufficiencyStatus.UNKNOWN,
        sufficiency_notes="Fallback mode: Limited extraction without LLM"
    )

    localization_card = LocalizationCard(
        version=1,
        updated_at=now,
        updated_by=updated_by,
        sufficiency_status=SufficiencyStatus.UNKNOWN
    )

    constraint_card = ConstraintCard(
        version=1,
        updated_at=now,
        updated_by=updated_by,
        sufficiency_status=SufficiencyStatus.UNKNOWN
    )

    structural_card = StructuralCard(
        version=1,
        updated_at=now,
        updated_by=updated_by,
        sufficiency_status=SufficiencyStatus.UNKNOWN
    )

    cards = {
        "symptom": symptom_card,
        "localization": localization_card,
        "constraint": constraint_card,
        "structural": structural_card
    }

    # 保存
    for card_type, card in cards.items():
        card_path = evidence_dir / f"{card_type}_card.json"
        with open(card_path, 'w', encoding='utf-8') as f:
            f.write(card.model_dump_json(indent=2))

    return {
        "cards": cards,
        "summary": {"phase": "Phase 1", "mode": "fallback", "note": "SDK not available"}
    }
