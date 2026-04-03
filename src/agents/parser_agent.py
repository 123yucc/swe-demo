"""
Parser sub-agent: reads 4 Markdown artifact files and extracts
structured EvidenceCards via the submit_extracted_evidence MCP tool.
"""

import asyncio
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from src.config import sdk_env
from src.models.context import EvidenceCards
from src.tools.ingestion_tools import (
    get_submitted_evidence,
    ingestion_server,
    reset_submitted_evidence,
)

PARSER_SYSTEM_PROMPT = """\
You are a precise and methodical software-defect analyst.

Your task is to read a set of issue artifact documents (provided in the user
message) and extract structured evidence into four multi-dimensional cards.
You MUST call the `mcp__ingestion__submit_extracted_evidence` tool exactly once
after you have read and analysed all documents.  Do NOT return evidence as plain
text — the only valid output is that tool call.

═══════════════════════════════════════════════════════════
CRITICAL: AS-IS vs TO-BE — You MUST distinguish these two states at all times.
═══════════════════════════════════════════════════════════

• AS-IS  = the CURRENT state of the codebase (what exists right now).
• TO-BE  = the DESIRED future state described in requirement / interface
           documents (e.g. new_interfaces.md, desired_*.md).

Rules:
1. Documents named new_interfaces.md, desired_*.md, expected_*.md, or any file
   that describes NEW APIs / behaviours to be implemented are TO-BE documents.
   The interfaces and methods they describe DO NOT yet exist in the codebase.

2. NEVER treat a TO-BE specification as an AS-IS fact.
   Example: if new_interfaces.md says "add set_detection_sensitivity()", that
   method does NOT exist yet — do NOT write that it "already exists".

3. In the constraint card, use the `behavioral_constraints` field to record
   TO-BE requirements with the explicit prefix "TO-BE: ", e.g.:
     "TO-BE: add method set_detection_sensitivity(min_face_size_factor, confidence_threshold_offset)"
   This marks it as something that must be ADDED, not something already present.

4. Extract ONLY what the documents explicitly state.  Do NOT infer, invent, or
   add capabilities that are not literally written in the documents.

═══════════════════════════════════════════════════════════
Multi-dimensional extraction guidelines:
═══════════════════════════════════════════════════════════

SYMPTOM CARD — extract from problem_statement.md and expected_and_current_behavior.md:
  • observable_failures: every visible symptom — error messages, exception types,
    stack traces, incorrect outputs, and any other observable anomalies.
  • repair_targets: what the fix should achieve — the expected correct behaviour.
  • regression_expectations: existing correct behaviours explicitly mentioned
    that MUST NOT break after the fix.

CONSTRAINT CARD — extract from requirements.md, new_interfaces.md:
  • semantic_boundaries: API contracts, documented constraints in docstrings
    or annotations, function signatures that must be respected.
  • behavioral_constraints: assertions, invariants, schema constraints, and
    TO-BE requirements (prefixed "TO-BE: ").
  • backward_compatibility: backward-compatibility requirements.
  • similar_implementation_patterns: if the documents mention existing similar
    APIs or patterns to follow, record them here.  Leave empty if none mentioned.

LOCALIZATION CARD — extract from problem_statement.md and any file that names
  specific code locations:
  • suspect_entities: suspected files, classes, functions, or variables.
  • exact_code_regions: leave EMPTY unless documents cite exact line numbers.
  • call_chain_context: leave EMPTY — will be filled by Deep Search.
  • dataflow_relevant_uses: leave EMPTY — will be filled by Deep Search.

STRUCTURAL CARD — extract from requirements.md, new_interfaces.md:
  • must_co_edit_relations: if the documents mention that changing one place
    requires updating another (e.g. "update both the model and the serializer"),
    record those pairs here.
  • dependency_propagation: cross-cutting dependencies mentioned in the docs
    (e.g. "config changes propagate to module X").

If the documents do not provide information for a field, leave it as an empty
list — do NOT guess.
"""


async def _prompt_stream(text: str):
    """Wrap a plain string as the async generator required by in-process MCP servers."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


async def _run_parser_async(md_contents: str) -> EvidenceCards:
    reset_submitted_evidence()

    tool_name = "mcp__ingestion__submit_extracted_evidence"
    options = ClaudeAgentOptions(
        system_prompt=PARSER_SYSTEM_PROMPT,
        mcp_servers={"ingestion": ingestion_server},
        allowed_tools=[tool_name],
        permission_mode="acceptEdits",
        env=sdk_env(),
    )

    async for _ in query(prompt=_prompt_stream(md_contents), options=options):
        pass  # drain the stream; side-effect: tool populates _submitted_evidence

    evidence = get_submitted_evidence()
    if evidence is None:
        raise RuntimeError("Parser agent did not call submit_extracted_evidence.")
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
