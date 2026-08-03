"""
Parser sub-agent: reads the SWE-bench Pro problem statement text and extracts
structured EvidenceCards via SDK structured output.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from src.agents._structured import run_structured_query
from src.agents.contract_parser import build_requirement_ledger, validate_ledger_coverage
from src.models.context import EvidenceCards
from src.models.evidence import ConstraintCard, LocalizationCard, StructuralCard, SymptomCard


PARSER_SYSTEM_PROMPT = """\
You are a software-defect analyst. Read the SWE-bench Pro problem statement
(may contain "Requirements:" and "New interfaces introduced:" sections) and
emit JSON matching the required schema. No Markdown, no prose, no guessing.

Fill ONLY these fields. Leave every other field as an empty list.

symptom.observable_failures
  Visible symptoms from the problem statement (errors, traces, wrong output).
symptom.repair_targets
  The fix's end-goal behavior, stated in the problem statement.
symptom.regression_expectations
  Correct behaviors the problem statement says MUST NOT break.

constraint.missing_elements_to_implement
  New API signatures from "New interfaces introduced:" (verbatim, one per entry).
  If the section is absent, leave empty.

requirements
  One RequirementItem per line in the "Requirements:" section (and per top-level
  expectation in "New interfaces introduced:"). For each item:
    - id:      "req-001", "req-002", ... in input order
    - text:    the verbatim requirement line (do NOT truncate or paraphrase)
    - origin:  "requirements" for items under Requirements:,
               "new_interfaces" for items under New interfaces introduced:.
               Do NOT use "problem_statement" — problem-statement facts belong
               in symptom.* instead.
    - verdict: always "UNCHECKED"
    - evidence_locations: []
    - findings: ""

schema_version: "v3".

All other fields (constraint.behavioral_constraints, semantic_boundaries,
backward_compatibility, similar_implementation_patterns, localization.*,
structural.*) are deep-search's responsibility. You MUST leave them empty.
"""


PARSER_COST_CONTROL_PROMPT = """\
You are a software-defect analyst. Read the SWE-bench Pro problem statement
and emit JSON matching the required schema. No Markdown, prose, or guessing.

Extract only:
- symptom.observable_failures: visible errors, traces, or wrong output.
- symptom.repair_targets: the stated end-goal behavior.
- symptom.regression_expectations: behavior explicitly required not to break.
- constraint.missing_elements_to_implement: verbatim API signatures from the
  "New interfaces introduced:" section, one entry per interface.

Do not reproduce the Requirements section. Contract extraction and all
deep-search-owned evidence are constructed deterministically by the caller.
"""


class _ParserConstraint(BaseModel):
    missing_elements_to_implement: list[str] = Field(default_factory=list)


class _ParserOutput(BaseModel):
    symptom: SymptomCard = Field(default_factory=SymptomCard)
    constraint: _ParserConstraint = Field(default_factory=_ParserConstraint)


_PARSER_FORBIDDEN_FIELDS: dict[str, tuple[str, ...]] = {
    "localization": (
        "suspect_entities",
        "exact_code_regions",
        "call_chain_context",
        "dataflow_relevant_uses",
    ),
    "structural": (
        "must_co_edit_relations",
        "dependency_propagation",
    ),
    "constraint": (
        "behavioral_constraints",
        "semantic_boundaries",
        "backward_compatibility",
        "similar_implementation_patterns",
    ),
}


def _enforce_parser_field_whitelist(evidence: EvidenceCards) -> None:
    """Force parser-output to only populate parser-owned fields.

    Phase 16 field-ownership rules: Parser owns symptom.*,
    constraint.missing_elements_to_implement, and requirements[].  Any
    deep-search-owned field accidentally filled by the parser is cleared
    and a warning is logged.
    """
    cleared: list[str] = []
    for card_name, field_names in _PARSER_FORBIDDEN_FIELDS.items():
        card = getattr(evidence, card_name)
        for field_name in field_names:
            if getattr(card, field_name):
                cleared.append(f"{card_name}.{field_name}")
                setattr(card, field_name, [])

    if cleared:
        print(
            f"[parser] field-whitelist: cleared deep-search-owned fields "
            f"the parser should not populate: {cleared}",
            flush=True,
        )


async def _run_parser_async(md_contents: str, cwd: str | None = None) -> EvidenceCards:
    parsed = await run_structured_query(
        system_prompt=PARSER_COST_CONTROL_PROMPT,
        user_prompt=md_contents,
        response_model=_ParserOutput,
        component="parser",
        allowed_tools=[],
        max_turns=10,
        max_budget_usd=1.0,
        cwd=cwd,
    )
    ledger = build_requirement_ledger(md_contents)
    evidence = EvidenceCards(
        symptom=parsed.symptom,
        constraint=ConstraintCard(
            missing_elements_to_implement=(
                parsed.constraint.missing_elements_to_implement
            ),
        ),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=ledger,
        schema_version="v3",
    )
    validate_ledger_coverage(md_contents, evidence.requirements)
    return evidence


def run_parser(md_contents: str) -> EvidenceCards:
    """Synchronous wrapper around the async parser agent.

    Args:
        md_contents: Concatenated Markdown text of all artifact files.

    Returns:
        Populated EvidenceCards instance.
    """
    return asyncio.run(_run_parser_async(md_contents))
