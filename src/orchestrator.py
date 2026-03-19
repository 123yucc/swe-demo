"""Orchestrator Agent - 主控制器。

负责协调各个 phase 的工作，管理子代理调用和会话状态。
参考 claude_sdk_docs/ 实现：
- 使用Agent子代理进行专门化任务
- 使用hooks进行证据收集和审计
- 使用structured outputs进行类型安全的结果提取
"""

import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

try:
    from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition
    CLAUDE_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_SDK_AVAILABLE = False
    # 创建 mock 类用于测试
    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.agents = kwargs.get('agents', {})
            self.allowed_tools = kwargs.get('allowed_tools', [])
            self.permission_mode = kwargs.get('permission_mode', 'default')
            self.output_format = kwargs.get('output_format', None)

    class AgentDefinition:
        def __init__(self, **kwargs):
            self.description = kwargs.get('description', '')
            self.prompt = kwargs.get('prompt', '')
            self.tools = kwargs.get('tools', [])
            self.model = kwargs.get('model', 'sonnet')


class Orchestrator:
    """Orchestrator Agent。

    负责整体流程控制：
    1. Phase 1: Artifact Parsing
    2. Phase 2: Evidence Extraction
    3. Phase 3: Closure Checking
    4. Phase 4: Patch Planning
    5. Phase 5: Patch Generation
    6. Phase 6: Validation/Replan
    """

    def __init__(self, workspace_dir: str, instance_id: str):
        self.workspace_dir = Path(workspace_dir)
        self.instance_id = instance_id
        self.instance_dir = self.workspace_dir / instance_id

        # 创建实例目录结构
        self._setup_instance_dirs()

        # 运行日志
        self.run_log_path = self.instance_dir / "run_log.jsonl"

    def _setup_instance_dirs(self):
        """设置实例目录结构。"""
        dirs = [
            self.instance_dir / "repo",
            self.instance_dir / "artifacts",
            self.instance_dir / "evidence",
            self.instance_dir / "evidence" / "card_versions",
            self.instance_dir / "closure",
            self.instance_dir / "plan",
            self.instance_dir / "patch"
        ]

        for dir_path in dirs:
            dir_path.mkdir(parents=True, exist_ok=True)

    def log_event(self, phase: str, event_type: str, data: Dict[str, Any]):
        """记录事件到运行日志。"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "instance_id": self.instance_id,
            "phase": phase,
            "event_type": event_type,
            "data": data
        }

        with open(self.run_log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry) + '\n')

    def get_agent_definitions(self) -> Dict[str, AgentDefinition]:
        """获取所有子代理定义。

        返回用于 Claude Agent SDK 的 AgentDefinition 配置。
        Phase 1 & 2 改进版本：
        - 使用LLM进行结构化信息提取
        - 支持动态置信度计算
        - 去除硬编码模式
        """
        return {
            "artifact-parser": AgentDefinition(
                description="Phase 1 specialist. Parses input artifacts and extracts structured evidence using LLM. "
                           "Use for: analyzing problem statements, requirements, interfaces and generating initial evidence cards.",
                prompt="""You are an Artifact Parser specialist for Phase 1 of evidence-based repair.

Your task is to parse input artifacts and extract structured evidence using LLM analysis.

Input artifacts to read:
- problem_statement.md: Contains issue description, error symptoms, stack traces
- requirements.md: Contains explicit requirements and constraints
- interface.md or new_interfaces.md: Contains API signatures, routes, entry points
- expected_and_current_behavior.md: Contains expected vs actual behavior comparison

For each artifact type, extract:

1. **Symptom Frame** (from problem_statement + expected_and_current_behavior):
   - observed_failure: {description, trigger_condition, exception_type, stack_trace_summary, error_message}
   - expected_behavior: {description, grounded_in}
   - mentioned_entities: list of {name, type, file_path?, line_number?}
   - hinted_scope: string describing scope

2. **Constraint Frame** (from requirements + interface):
   - must_do: list of required behaviors
   - must_not_break: list of compatibility requirements
   - allowed_behavior: list of permitted behaviors
   - forbidden_behavior: list of prohibited behaviors
   - compatibility_expectations: list of backward compatibility requirements
   - edge_case_obligations: list of edge cases to handle

3. **Interface Boundary** (from interface/new_interfaces):
   - API signatures with routes, methods, parameters
   - Entry points that must not change
   - Response format requirements

4. **Sufficiency Assessment** (Phase 1: only check existence):
   - Check if each artifact file exists
   - Record missing artifacts in sufficiency_notes
   - Set sufficiency_status to "partial" if critical artifacts missing

Evidence sources to record:
- For each extracted item, record evidence_source with source_type and source_path
- Confidence is computed later, not hardcoded

Output format: Generate 4 evidence cards as JSON files in evidence/ directory:
- symptom_card.json (v1)
- localization_card.json (v1 - initial anchors only)
- constraint_card.json (v1)
- structural_card.json (v1 - API signatures only)

Also save version history to evidence/card_versions/v1/""",
                tools=["Read", "Glob", "Write", "Bash"],
                model="sonnet"
            ),

            "symptom-extractor": AgentDefinition(
                description="Phase 2 specialist. Analyzes symptoms from tests/logs with dynamic extraction. "
                           "Use for: extracting error patterns, analyzing trigger conditions, assessing symptom sufficiency.",
                prompt="""You are a Phase 2 Symptom Evidence specialist.

Your task is to deeply analyze symptoms using artifacts and repository analysis.

Steps:
1. Read the Phase 1 symptom_card.json for initial data
2. If tests/ directory exists, search for test files that reproduce the issue
3. If logs/ directory exists, analyze log files for error patterns
4. Use Grep to find error messages, stack traces in the codebase

Extract dynamically (not hardcoded):
- Error patterns from actual code/text, not predefined lists
- Trigger conditions from test cases or log analysis
- Stack trace locations with file paths and line numbers

Use AST analysis to locate:
- Where exceptions are raised
- Where errors are logged
- Function call chains leading to failures

Sufficiency assessment:
- Check: has reproducible failure? has clear trigger? has expected behavior?
- If missing: set status to "partial" and record specific gaps

Update symptom_card.json to v2 with:
- Enhanced observed_failure with evidence_source
- Detailed trigger analysis
- Dynamic confidence (will be computed, not hardcoded)
- sufficiency_status and sufficiency_notes

Evidence sources to record for each finding:
- source_type: "artifact", "test", "log", "repo", "ast"
- source_path: file path
- matching_detail: relevant text/match info""",
                tools=["Read", "Grep", "Glob", "Bash"],
                model="sonnet"
            ),

            "localization-extractor": AgentDefinition(
                description="Phase 2 specialist. Finds candidate edit locations using text+AST analysis. "
                           "Use for: mapping anchors to code, finding candidate locations, computing confidence.",
                prompt="""You are a Phase 2 Localization Evidence specialist.

Your task is to precisely locate code that needs modification.

Input:
- Phase 1 localization_card.json with initial anchors
- Phase 1 symptom_card.json with mentioned_entities
- Repository at repo/ directory

Analysis approach (dynamic, not hardcoded):

1. **Text-based Grep Search**:
   - Use mentioned_entities (routes, function names, class names) as search keys
   - Search repo for matches using Grep
   - Record file paths and line numbers

2. **AST-based Analysis** (use Python/bash):
   - Find function/class definitions with decorators (@app.route, etc.)
   - Locate call sites and usage patterns
   - Identify imports and dependencies
   - Use: `python -c "import ast; ..."` or `rg` for pattern matching

3. **Generate Candidate Locations**:
   For each match, record:
   - file_path: relative path from repo root
   - symbol_name: function/class name
   - symbol_type: function/class/method
   - region_start, region_end: line numbers
   - evidence_source: list of EvidenceSource objects
   - computed_confidence: calculate based on:
     * Source authority: interface > mentioned > heuristic
     * Match precision: exact > fuzzy
     * LLM assessment: 0-1 score

4. **Generate Mappings**:
   - interface_to_code_mappings: map routes to file/function
   - test_to_code_mappings: if tests exist, map to code under test

Sufficiency assessment:
- Check: has primary location? has secondary/supporting locations? maps interface?
- If missing, set partial/insufficient and note gaps

Update localization_card.json to v2 with:
- Complete candidate_locations with dynamic confidence
- interface_to_code_mappings
- test_to_code_mappings (or note if tests unavailable)
- sufficiency assessment

IMPORTANT: Remove all hardcoded patterns like "enroll_batch -> FaceDetector".
Everything must be derived from actual artifact content and code analysis.""",
                tools=["Read", "Grep", "Glob", "Bash"],
                model="sonnet"
            ),

            "constraint-extractor": AgentDefinition(
                description="Phase 2 specialist. Extracts constraints from requirements, interfaces, and code. "
                           "Use for: analyzing type hints, docstrings, schema constraints, compatibility requirements.",
                prompt="""You are a Phase 2 Constraint Evidence specialist.

Your task is to extract all constraints that a fix must satisfy.

Input:
- Phase 1 constraint_card.json
- requirements.md and interface.md artifacts
- Repository code at repo/

Constraint extraction (dynamic analysis):

1. **From Requirements and Interface**:
   - API signatures: Extract from interface specs
   - Route definitions: Map HTTP methods to handlers
   - Decorator analysis: @app.route, @validate, etc.
   - Response format requirements: JSON schema, status codes

2. **From Code Analysis**:
   - Type hints: Function signatures, Pydantic models, dataclasses
   - Docstring constraints: Parameter descriptions, return values
   - Assert statements: Pre/post conditions
   - Schema definitions: JSON schema, config files

3. **Constraint Categories**:
   - must_do: Required behaviors from requirements
   - must_not_break: Backward compatibility requirements
   - allowed_behavior: Permitted variations
   - forbidden_behavior: Prohibited actions
   - compatibility_expectations: Version compatibility
   - edge_case_obligations: Edge cases to handle

4. **Type Constraints**:
   - Extract from function signatures
   - Extract from Pydantic/dataclass definitions
   - Record in type_constraints dict

Sufficiency assessment:
- Check: has API constraints? has type constraints? has edge case requirements?
- Note any missing: "未找到接口实现" / "缺少schema定义"

Update constraint_card.json to v2 with:
- All constraint categories populated from dynamic analysis
- api_signatures and type_constraints
- sufficiency assessment

Evidence sources to record for each constraint:
- source_type: "requirement", "interface", "type_hint", "docstring", "assertion", "schema"
- source_path: file where found
- matching_detail: relevant text""",
                tools=["Read", "Grep", "Glob", "Bash"],
                model="sonnet"
            ),

            "structural-extractor": AgentDefinition(
                description="Phase 2 specialist. Analyzes code dependencies and structural relationships. "
                           "Use for: building call graphs, identifying co-edit groups, assessing propagation risks.",
                prompt="""You are a Phase 2 Structural Evidence specialist.

Your task is to analyze code structure and dependencies.

Input:
- Phase 2 localization_card.json with candidate_locations
- Repository code at repo/

Structural analysis (dynamic, not hardcoded):

1. **Dependency Analysis** using AST:
   - Caller-callee relationships: Find what calls the candidate locations
   - Import chains: Trace imports to understand dependencies
   - Cross-file references: Use Grep to find references

   Use Python AST or grep:
   ```bash
   rg "from X import Y|import X" --type py
   python -c "import ast; ... analyze call graph ..."
   ```

2. **Wrapper/Adapter Pattern Detection**:
   - Find wrapper functions that delegate to others
   - Identify adapter patterns
   - Detect facade patterns

3. **Co-edit Group Identification**:
   Look for patterns that suggest co-editing:
   - Handler + helper functions in same path
   - Read/write pairs for same data structure
   - Request/response format pairs
   - Same error handling pattern in multiple places

   For each group, record:
   - group_id: unique identifier
   - entities: list of related symbols
   - reason: why they must be edited together
   - evidence_source

4. **Propagation Risk Assessment**:
   - Analyze dependency edges to find risks
   - Identify public API changes that affect consumers
   - Note config changes that affect behavior

Sufficiency assessment:
- Check: has dependency analysis? has co-edit groups? has risk assessment?

Update structural_card.json to v2 with:
- dependency_edges: list of DependencyEdge with evidence_source
- co_edit_groups: list of CoEditGroup
- propagation_risks: list of risk descriptions
- sufficiency assessment

IMPORTANT: Remove all hardcoded patterns.
Do NOT include hardcoded "enroll_batch -> FaceDetector" edges.
All dependencies must be discovered through actual code analysis.""",
                tools=["Read", "Grep", "Glob", "Bash"],
                model="sonnet"
            ),

            "closure-checker": AgentDefinition(
                description="Phase 3 specialist. Evaluates evidence closure before patch planning. "
                           "Use for: checking sufficiency, consistency, and correct attribution across all evidence cards.",
                prompt="""You are a Closure Checker specialist for Phase 3.

Your task is to evaluate whether evidence is sufficient to proceed to patch planning.

Read all evidence cards:
- evidence/symptom_card.json (v2)
- evidence/localization_card.json (v2)
- evidence/constraint_card.json (v2)
- evidence/structural_card.json (v2)

Evaluation criteria:

1. **Sufficiency Check**:
   - Symptom: Are failure and expected behavior clear? Are test targets identified?
   - Localization: Are candidate locations precise (file+symbol+region)?
   - Constraint: Are all requirements/API constraints extracted?
   - Structural: Are dependencies and co-edit requirements analyzed?

2. **Consistency Check** (cross-card validation):
   - Symptom ↔ Localization: Do locations explain symptoms?
   - Localization ↔ Constraint: Can locations satisfy constraints?
   - Constraint ↔ Structural: Are constraints achievable given structure?
   - Symptom ↔ Structural: Can the structure explain all symptoms?

3. **Attribution Check**:
   - Is symptom attributed to root cause, not surface symptom?
   - Do locations point to actual responsible code?
   - Does structural evidence support this attribution?

Output:
- closure_report.json with:
  - sufficiency_check: {symptom, localization, constraint, structural}
  - consistency_check: {pairwise_checks}
  - attribution_check: {assessment}
  - overall_status: "pass" | "fail"
  - gaps: list of specific evidence gaps if failed

If closure fails, identify specific gaps and guide evidence gathering.
You are the gatekeeper. Only pass when evidence supports confident repair.""",
                tools=["Read"],
                model="sonnet"
            ),

            "patch-planner": AgentDefinition(
                description="Phase 4 specialist. Creates detailed patch plans based on evidence. "
                           "Use for: generating edit plans with locations, changes, and rationales.",
                prompt="""You are a Patch Planner specialist for Phase 4.

Your task is to create a detailed patch plan based on evidence cards.

Read all evidence cards from evidence/ directory.

Create patch plan with:
1. **Edit Specifications**:
   - file_path: target file
   - symbol_name: function/class to modify
   - line_range: start-end lines
   - change_type: "modify" | "add" | "remove"
   - change_description: what to change
   - rationale: which evidence supports this change

2. **Edit Groups**:
   - Group related edits (from structural co_edit_groups)
   - Specify order of edits if important

3. **Validation Plan**:
   - What to verify after patch
   - Edge cases to check
   - Test scenarios

Save to plan/patch_plan.json""",
                tools=["Read", "Write"],
                model="sonnet"
            ),

            "patch-executor": AgentDefinition(
                description="Phase 5 specialist. Implements patches according to plan. "
                           "Use for: making code changes and generating final patch files.",
                prompt="""You are a Patch Executor specialist for Phase 5.

Your task is to implement patches according to the patch plan.

Read plan/patch_plan.json

For each edit:
1. Read the target file
2. Make precise changes using Edit tool
3. Preserve code style and formatting
4. Update imports if needed

After all edits:
1. Verify syntax is correct
2. Generate final patch file in .pred format
3. Save to patch/ directory""",
                tools=["Read", "Edit", "Write", "Bash"],
                model="sonnet"
            )
        }

    def get_phase1_workflow_prompt(self) -> str:
        """获取 Phase 1 的工作流程提示词。"""
        return """Phase 1: Artifact Parsing

We are starting the evidence-based repair process. Your task is to parse all input artifacts and generate initial evidence cards.

Steps:
1. Use the artifact-parser agent to parse all artifacts in the artifacts/ directory
2. Generate initial evidence cards:
   - evidence/symptom_card.json
   - evidence/localization_card.json
   - evidence/constraint_card.json
   - evidence/structural_card.json
3. Log the parsing results

Please begin by invoking the artifact-parser agent with the Task tool."""

    def create_options(self, phase: str = "full") -> ClaudeAgentOptions:
        """创建用于 query() 的 options 配置。

        Args:
            phase: "phase1", "phase2", or "full" - 控制允许的工具集
        """
        # 根据阶段配置工具
        if phase == "phase1":
            allowed_tools = ["Read", "Glob", "Write"]
            permission_mode = "default"  # Phase1 read-only mostly
        elif phase == "phase2":
            allowed_tools = ["Read", "Grep", "Glob", "Bash"]
            permission_mode = "acceptEdits"
        else:
            allowed_tools = ["Task", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"]
            permission_mode = "acceptEdits"

        return ClaudeAgentOptions(
            agents=self.get_agent_definitions(),
            allowed_tools=allowed_tools,
            permission_mode=permission_mode
        )

    def get_hooks_config(self) -> Optional[Dict[str, Any]]:
        """获取hooks配置用于证据收集和审计。

        返回用于 PreToolUse 和 PostToolUse 的 hooks 配置。
        """
        return {
            "PreToolUse": [
                # 可以在这里添加验证hook
            ],
            "PostToolUse": [
                # 可以在这里添加审计hook
            ]
        }


async def run_repair_workflow(
    workspace_dir: str,
    instance_id: str,
    prompt: str
) -> Dict[str, Any]:
    """运行完整的修复工作流。

    Args:
        workspace_dir: 工作目录
        instance_id: 实例 ID
        prompt: 初始问题描述

    Returns:
        工作流执行结果
    """
    from claude_agent_sdk import query

    orchestrator = Orchestrator(workspace_dir, instance_id)
    orchestrator.log_event("start", "workflow_init", {"prompt": prompt})

    options = orchestrator.create_options()
    full_prompt = f"""{orchestrator.get_phase1_workflow_prompt()}

Instance ID: {instance_id}
Workspace: {workspace_dir}

{prompt}
"""

    results = []
    async for message in query(prompt=full_prompt, options=options):
        results.append(message)
        # 记录重要事件
        if hasattr(message, 'result'):
            orchestrator.log_event("execution", "agent_result", {
                "result": str(message.result)
            })

    orchestrator.log_event("complete", "workflow_finish", {
        "total_messages": len(results)
    })

    return {
        "instance_id": instance_id,
        "messages": results,
        "log_file": str(orchestrator.run_log_path)
    }
