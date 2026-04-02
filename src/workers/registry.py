"""Worker registry built from unified WorkerSpec contracts."""

from __future__ import annotations

from typing import Dict

from ..contracts.workflow import WorkerExecutionMode, WorkerSpec


def create_default_worker_specs() -> Dict[str, WorkerSpec]:
    """Create default worker specs from one source."""
    specs: Dict[str, WorkerSpec] = {}

    def add(spec: WorkerSpec) -> None:
        specs[spec.worker_id] = spec

    add(
        WorkerSpec(
            worker_id="artifact-parser",
            phase="phase1",
            description="Parse input artifacts and generate initial evidence cards",
            produces_cards=["symptom", "localization", "constraint", "structural"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Phase1 parser: extract structured evidence cards from artifacts.",
            allowed_tools=["Read", "Glob", "Write", "Bash"],
            executor="run_phase1_parsing",
        )
    )

    add(
        WorkerSpec(
            worker_id="symptom-extractor",
            phase="phase2",
            description="Extract and enhance symptom evidence",
            depends_on=["artifact-parser"],
            produces_cards=["symptom"],
            can_parallel=True,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Phase2 symptom extraction and evidence enhancement.",
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            executor="extract_symptom_evidence",
        )
    )

    add(
        WorkerSpec(
            worker_id="localization-extractor",
            phase="phase2",
            description="Extract and enhance localization evidence",
            depends_on=["artifact-parser"],
            produces_cards=["localization"],
            can_parallel=True,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Phase2 localization extraction and candidate ranking.",
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            executor="extract_localization_evidence",
        )
    )

    add(
        WorkerSpec(
            worker_id="constraint-extractor",
            phase="phase2",
            description="Extract and enhance constraint evidence",
            depends_on=["artifact-parser"],
            produces_cards=["constraint"],
            can_parallel=True,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Phase2 constraint extraction from requirements/code/contracts.",
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            executor="extract_constraint_evidence",
        )
    )

    add(
        WorkerSpec(
            worker_id="structural-extractor",
            phase="phase2",
            description="Extract and enhance structural evidence",
            depends_on=["artifact-parser"],
            produces_cards=["structural"],
            can_parallel=True,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Phase2 structural analysis for dependency and co-edit groups.",
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            executor="extract_structural_evidence",
        )
    )

    add(
        WorkerSpec(
            worker_id="llm-enhancer",
            phase="phase2",
            description="LLM enhancement for all evidence cards",
            depends_on=["symptom-extractor", "localization-extractor", "constraint-extractor", "structural-extractor"],
            produces_cards=["symptom", "localization", "constraint", "structural"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Cross-card evidence enhancement and consistency polishing.",
            allowed_tools=["Read", "Write"],
            executor="enhance_all_cards",
        )
    )

    add(
        WorkerSpec(
            worker_id="closure-checker",
            phase="phase3",
            description="Check evidence closure before patch planning",
            depends_on=["llm-enhancer"],
            gate_conditions=["evidence_sufficient"],
            produces_todos=["gap_verification"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Evaluate evidence closure and emit gap todos when blocked.",
            allowed_tools=["Read"],
            executor="check_evidence_closure",
        )
    )

    add(
        WorkerSpec(
            worker_id="patch-planner",
            phase="phase4",
            description="Create detailed patch plan",
            depends_on=["closure-checker"],
            gate_conditions=["closure_passed"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Generate patch plan from evidence cards.",
            allowed_tools=["Read", "Write"],
            executor="create_patch_plan",
        )
    )

    add(
        WorkerSpec(
            worker_id="patch-executor",
            phase="phase5",
            description="Execute patch plan",
            depends_on=["patch-planner"],
            gate_conditions=["plan_valid"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Apply patch plan to repository and write outputs.",
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            executor="execute_patch",
        )
    )

    add(
        WorkerSpec(
            worker_id="validator",
            phase="phase6",
            description="Validate patch results",
            depends_on=["patch-executor"],
            can_parallel=False,
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template="Run validation and return pass/fail with feedback.",
            allowed_tools=["Read", "Bash"],
            executor="validate_patch",
        )
    )

    return specs


def create_default_registry() -> Dict[str, WorkerSpec]:
    """Compatibility alias for legacy imports."""
    return create_default_worker_specs()
