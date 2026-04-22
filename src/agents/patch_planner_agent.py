"""
Patch Planner sub-agent (phase 18.D): reads EvidenceCards and produces a
structured PatchPlan with preserved_findings for constraint propagation.

Structured output is produced via src/agents/_structured.py.
"""

import asyncio
import re

from src.agents._structured import run_structured_query
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan

# Prescriptive patterns that indicate boundary constraints to preserve.
_PRESCRIPTIVE_PATTERNS = (
    re.compile(r"`[^`]+`"),  # backtick-enclosed code
    re.compile(r"correct (?:form|value|comparison) is?\s*[:\s]+"),
    re.compile(r"should be\s*[:\s]+"),
    re.compile(r"must be\s*[:\s]+"),
    re.compile(r"must use\s+"),
    re.compile(r"instead of\s+"),
    re.compile(r"change\s+\w+\s+to\s+"),
    re.compile(r"replace\s+\w+\s+with\s+"),
    re.compile(r"correct|should be|must be|正确|应改为"),
    re.compile(r"\(\s*\w+\s+\|\|\s+Date\.now\(\)\s*\)"),  # specific ttl formula
    re.compile(r"ttl\s*\|\|\s*Date\.now\(\)"),
)


def _extract_prescriptive_snippets(findings: str) -> list[str]:
    """Extract prescriptive snippets from findings that must be preserved."""
    snippets: list[str] = []
    # Extract backtick-enclosed tokens
    for m in re.finditer(r"`([^`]+)`", findings):
        snippet = m.group(1).strip()
        if len(snippet) >= 3 and snippet not in [s for s in snippets]:
            snippets.append(snippet)
    # Extract lines with prescriptive keywords
    for line in findings.split("\n"):
        lower = line.lower()
        if any(kw in lower for kw in ("correct", "should be", "must be", "正确", "应改为")):
            line = line.strip()
            if line and line not in snippets:
                snippets.append(line)
    return snippets


PATCH_PLANNER_SYSTEM_PROMPT = """\
You are a Senior Staff Engineer planning a precise bug fix.

Review evidence cards and cached code, then produce a strategic edit plan.
Focus on: exact_code_regions, call_chain_context, behavioral_constraints,
backward_compatibility, missing_elements_to_implement, co_edit_relations.

CRITICAL — preserved_findings (phase 18.D):
For each FileEditPlan, you MUST populate the `preserved_findings` field
with verbatim prescriptive snippets from RequirementItem.findings that
apply to this file.  Copy these EXACTLY — do not summarize or paraphrase.

Prescriptive snippets include:
- Backtick-enclosed code tokens (e.g. `db.mget`, `ttl || Date.now()`)
- Lines containing "correct is", "should be", "must be", "instead of"
- Specific formula or comparison expressions

Example GOOD preserved_findings:
  ["`ttl || Date.now() + interval > max`", "correct comparison: (ttl || Date.now()) + interval > max"]

Example BAD (summarized, not preserved):
  ["use correct ttl formula"]   ← paraphrased, loses the formula

Rules:
- EVIDENCE-GROUNDED: every file justified by evidence
- MINIMAL & SUFFICIENT: smallest change set that fully fixes the defect
- ORDER: list edits in dependency order (dependencies first)
- NO CODE: describe *what* and *why*, not actual code
- TO-BE items in constraints describe behaviors to ADD, not existing ones
- preserved_findings: copy verbatim, never summarize

Return a structured JSON object matching the required schema.
"""


async def _run_patch_planner_async(memory: SharedWorkingMemory) -> PatchPlan:
    prompt = (
        "Plan a bug fix based on the following context:\n\n"
        f"{memory.format_for_prompt()}\n\n"
        "Return a structured patch plan with preserved_findings per file."
    )

    plan = await run_structured_query(
        system_prompt=PATCH_PLANNER_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=PatchPlan,
        component="patch-planner",
        allowed_tools=[],
        max_turns=10,
        max_budget_usd=1.5,
    )

    # ── Phase 18.D: Ensure preserved_findings populated from findings ──
    # If the model did not fill preserved_findings, backfill from requirements.
    for edit in plan.edits:
        if not edit.preserved_findings:
            for req in memory.evidence_cards.requirements:
                if req.findings:
                    snippets = _extract_prescriptive_snippets(req.findings)
                    for snippet in snippets:
                        if snippet not in edit.preserved_findings:
                            edit.preserved_findings.append(snippet)

    memory.patch_plan = plan
    return plan


def run_patch_planner(memory: SharedWorkingMemory) -> PatchPlan:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with evidence cards and cached code.

    Returns:
        PatchPlan with per-file edit intents.
    """
    return asyncio.run(_run_patch_planner_async(memory))
