"""Worker and subagent registry configuration."""

from __future__ import annotations

from typing import Dict

from ..contracts.workflow import WorkerExecutionMode, WorkerSpec

MAIN_PROMPT_TEMPLATE = "Build evidence cards for instance {instance_id}."
PHASE1_ONLY_SUFFIX = " Run only phase 1 artifact parsing."
PHASE2_ONLY_SUFFIX = " Run only phase 2 evidence refinement."

_BASE_QUALITY_RULES = """
Core rules:
- Use only evidence observed from repository files and artifacts.
- Never invent file paths, symbols, stack traces, API signatures, or test names.
- Prefer specific citations over generic statements.
- If evidence is incomplete, keep fields conservative and explain gaps in sufficiency_notes.
""".strip()

AGENT_DEFINITIONS = {
    "step1_symptom_extractor": {
        "description": "Step 1 specialist. Extract a structural Symptom Frame from the Problem Statement.",
        "prompt": (
            "You are the Step 1 Symptom Extractor. Parse the problem statement (which is explicitly augmented for independent resolution). "
            "Your output must construct an initial Symptom Evidence, including:\n"
            "- observed_failure: What is currently broken.\n"
            "- trigger_condition: Under what conditions it fails.\n"
            "- expected_behavior: How it should behave according to the problem report.\n"
            "- mentioned_entities: Specific module, class, function, route, config key, feature names mentioned.\n"
            "- hinted_scope: The likely involved subsystem or component.\n"
            f"{_BASE_QUALITY_RULES}"
        ),
        "tools": ["Read", "Grep"],
    },
    "step2_constraint_extractor": {
        "description": "Step 2 specialist. Extract explicit correctness constraints from Requirements.",
        "prompt": (
            "You are the Step 2 Constraint Extractor. Translate requirements into explicit correctness constraints. "
            "Requirements are highly grounded on validation tests. Do not treat them as soft prompts, but as Constraints.\n"
            "Produce:\n"
            "- must_do & must_not_break.\n"
            "- allowed_behavior & forbidden_behavior.\n"
            "- compatibility_expectations & edge_case_obligations (e.g. backward compatibility, expected API returns).\n"
            f"{_BASE_QUALITY_RULES}"
        ),
        "tools": ["Read", "Grep"],
    },
    "step3_boundary_extractor": {
        "description": "Step 3 specialist. Extract repair boundaries from the Interface spec.",
        "prompt": (
            "You are the Step 3 Boundary Extractor. Extract explicit interface boundaries from interface specifications. "
            "Identify the boundaries that the patch must respect and the code locations it must touch.\n"
            "This bridges Localization and Constraints. Extract:\n"
            "- Class names, function names, and route names.\n"
            "- Expected entry points to be exposed.\n"
            "- Symbol names that must NOT be altered incorrectly.\n"
            f"{_BASE_QUALITY_RULES}"
        ),
        "tools": ["Read", "Grep"],
    },
    "step4_repo_explorer": {
        "description": "Step 4 specialist. Perform targeted code extraction (Localization and Structural) based on step 1-3 anchors.",
        "prompt": (
            "You are the Step 4 Repo Explorer. Your task is to perform targeted extraction within the repository, driven by anchors.\n"
            "You DO NOT read randomly. You use the provided `mentioned_entities` and `entry_points` from previous steps as anchors.\n"
            "\nFor Localization:\n"
            "Find candidate files, symbols, edit regions, call chain neighborhoods, and dataflow-relevant code.\n"
            "\nFor Structural:\n"
            "Based on the localization candidates, extract caller-callee links, import chains, config/code linkages, and must-co-edit groupings.\n"
            "Answer: Where should the patch land? What else must change synchronously?\n"
            f"{_BASE_QUALITY_RULES}"
        ),
        "tools": ["Read", "Grep", "Glob"],
    },
}


def create_default_worker_specs() -> Dict[str, WorkerSpec]:
    """Create default runtime worker specs for orchestration."""
    return {
        "step1_symptom_extractor": WorkerSpec(
            worker_id="step1_symptom_extractor",
            phase="extract",
            description=AGENT_DEFINITIONS["step1_symptom_extractor"]["description"],
            can_parallel=True,
            produces_cards=["symptom"],
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template=AGENT_DEFINITIONS["step1_symptom_extractor"]["prompt"],
            allowed_tools=AGENT_DEFINITIONS["step1_symptom_extractor"]["tools"],
        ),
        "step2_constraint_extractor": WorkerSpec(
            worker_id="step2_constraint_extractor",
            phase="extract",
            description=AGENT_DEFINITIONS["step2_constraint_extractor"]["description"],
            can_parallel=True,
            produces_cards=["constraint"],
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template=AGENT_DEFINITIONS["step2_constraint_extractor"]["prompt"],
            allowed_tools=AGENT_DEFINITIONS["step2_constraint_extractor"]["tools"],
        ),
        "step3_boundary_extractor": WorkerSpec(
            worker_id="step3_boundary_extractor",
            phase="extract",
            description=AGENT_DEFINITIONS["step3_boundary_extractor"]["description"],
            can_parallel=True,
            produces_cards=["boundary"],
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template=AGENT_DEFINITIONS["step3_boundary_extractor"]["prompt"],
            allowed_tools=AGENT_DEFINITIONS["step3_boundary_extractor"]["tools"],
        ),
        "step4_repo_explorer": WorkerSpec(
            worker_id="step4_repo_explorer",
            phase="explore",
            description=AGENT_DEFINITIONS["step4_repo_explorer"]["description"],
            depends_on=["step1_symptom_extractor", "step2_constraint_extractor", "step3_boundary_extractor"],
            can_parallel=False,
            produces_cards=["localization", "structural"],
            execution_mode=WorkerExecutionMode.CLAUDE_AGENT,
            model="sonnet",
            prompt_template=AGENT_DEFINITIONS["step4_repo_explorer"]["prompt"],
            allowed_tools=AGENT_DEFINITIONS["step4_repo_explorer"]["tools"],
        ),
    }


def create_default_registry() -> Dict[str, WorkerSpec]:
    """Backward-compatible alias for registry consumers."""
    return create_default_worker_specs()
