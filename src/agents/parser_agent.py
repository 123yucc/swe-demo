"""
Parser sub-agent: reads the SWE-bench Pro problem statement text and extracts
structured EvidenceCards via SDK structured output.
"""

import asyncio
from pathlib import Path

from src.agents._structured import run_structured_query
from src.models.context import EvidenceCards


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

schema_version: "v2".

All other fields (constraint.behavioral_constraints, semantic_boundaries,
backward_compatibility, similar_implementation_patterns, localization.*,
structural.*) are deep-search's responsibility. You MUST leave them empty.
"""


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
    evidence = await run_structured_query(
        system_prompt=PARSER_SYSTEM_PROMPT,
        user_prompt=md_contents,
        response_model=EvidenceCards,
        component="parser",
        allowed_tools=[],
        max_turns=10,
        max_budget_usd=1.0,
        cwd=cwd,
    )
    _enforce_parser_field_whitelist(evidence)
    return evidence


def run_parser(md_contents: str) -> EvidenceCards:
    """Synchronous wrapper around the async parser agent.

    Args:
        md_contents: Concatenated Markdown text of all artifact files.

    Returns:
        Populated EvidenceCards instance.
    """
    return asyncio.run(_run_parser_async(md_contents))


def load_artifacts(artifacts_dir: str | Path) -> str:
    """Read all .md files in *artifacts_dir* and concatenate them."""
    artifacts_dir = Path(artifacts_dir)
    parts: list[str] = []
    for md_file in sorted(artifacts_dir.glob("*.md")):
        parts.append(f"=== {md_file.name} ===\n{md_file.read_text(encoding='utf-8')}\n")
    if not parts:
        raise FileNotFoundError(f"No .md files found in {artifacts_dir}")
    return "\n".join(parts)
