"""
Orchestrator: drives the evidence-closure loop.

The orchestrator runs as a Claude Agent SDK agent that acts as an
Information Foraging Orchestrator — it continuously assesses the four
evidence cards for gaps and dispatches Deep Search tasks until no
evidence is still missing.

State flow:
  Init → (Parser) → UnderSpecified ↔ (Deep Search) → Evidence Refining → Closed
"""

import asyncio
import re
from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query

from src.agents.parser_agent import load_artifacts, _run_parser_async
from src.agents.deep_search_agent import DEEP_SEARCH_SYSTEM_PROMPT
from src.config import sdk_env
from src.tools.ingestion_tools import (
    evidence_server,
    get_submitted_evidence,
    set_evidence_json_path,
)

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are an Information Foraging Orchestrator.  Your sole objective is to
fill all four evidence cards until no evidence is still missing, then
declare closure.

The initial EvidenceCards (parsed from artifacts) are provided in the user
message.  Your job is to assess them and drive the investigation to closure.

═══════════════════════════════════════════════════════════
GAP-FILLING LOOP (use TodoWrite to track all tasks)
═══════════════════════════════════════════════════════════

After every Deep Search return, re-assess the current EvidenceCards state:

1. CHECK SYMPTOM GAPS
   - Are observable_failures populated?  (e.g. do we have stack traces?)
   - Are repair_targets clear?
   → If gaps exist, dispatch a Deep Search TODO.

2. CHECK LOCALIZATION GAPS
   - Does suspect_entities have at least one concrete file AND function?
   - Are exact_code_regions still empty?  → dispatch Deep Search.
   - Is call_chain_context empty?  If we have a suspect function, dispatch
     a Deep Search TODO: "Find callers of <function> to build call chain."
   - Is dataflow_relevant_uses empty?  If suspect function modifies a data
     structure, dispatch: "Trace Def-Use of <variable>."

3. CHECK CONSTRAINT GAPS
   - Are behavioral_constraints populated?
   - Does the document mention similar APIs but similar_implementation_patterns
     is empty?  → dispatch Deep Search to find existing similar implementations.

4. CHECK STRUCTURAL GAPS
   - Are must_co_edit_relations empty despite localization having suspects?
     → dispatch: "Find callers / dependents of <entity> for co-edit analysis."
   - Is dependency_propagation empty?
     → dispatch: "Trace interface / config dependencies of <module>."

5. READY FOR CLOSURE CHECK
   When ALL key fields in ALL four cards contain real data, AND you cannot
   formulate any more concrete, executable Deep Search tasks based on
   current card information, mark all Todos as done and proceed to closure.

   BOUNDARY RULE: Your job is to fill the cards with facts.  Do NOT try to
   judge "which information is harmful or irrelevant to the bug" — that is
   a downstream Closure Checker's responsibility.  You only ensure that the
   cards have evidence, not that the evidence solves the bug.

═══════════════════════════════════════════════════════════
PERSISTING FINDINGS — MANDATORY after every Deep Search return
═══════════════════════════════════════════════════════════

Immediately after reading each Deep Search report:
a. Parse the "EXACT_LINES" block from the report.
b. Call `mcp__evidence__update_localization` with exact_code_regions (plus
   any confirmed suspect_entities, call_chain_context, dataflow data).
c. Do this BEFORE re-assessing closure — persisting is mandatory.

═══════════════════════════════════════════════════════════
CLOSURE — when ready, produce a final Markdown summary containing:
═══════════════════════════════════════════════════════════

- Confirmed defect location (file, function, line range)
- Root cause description
- Constraints that must be respected by the fix
- Call chain context around the defect
- Co-edit locations that must be updated together
- Suggested fix approach (strictly limited to what the requirements specify)
- A machine-readable fenced block in EXACTLY this format (required):

  ## EXACT_LINES (machine-readable)
  ```
  path/to/file.py:LINE
  path/to/file.py:LINE-LINE
  ```

  Every confirmed defect line must appear in that block.  Omitting it will
  cause the pipeline to treat the exact_code_regions field as empty.

═══════════════════════════════════════════════════════════
HARD CLOSURE RULES — violating any of these blocks closure:
═══════════════════════════════════════════════════════════

RULE 1 — exact_code_regions must NOT be empty before declaring closure.
  If exact_code_regions is still [] after a Deep Search reported concrete
  line numbers, it means you have NOT yet called update_localization.
  You MUST call update_localization first, then verify.

RULE 2 — Localization must have at least one concrete file AND one
  concrete function in suspect_entities before declaring closure.

RULE 3 — Do NOT declare closure based on TO-BE items in the constraint card.
  Items prefixed "TO-BE: " are things that need to be ADDED; they are not
  evidence that the defect is located.

RULE 4 — The suggested fix must address ONLY what the requirements specify.
  Do NOT add adaptive logic, dynamic computation, or other enhancements
  that are not explicitly demanded by the constraint card.

RULE 5 — FACT-ALIGNMENT: When writing the final closure report, every
  factual claim about the codebase MUST be grounded in what Deep Search
  actually found, not inferred from requirements.

  Specifically:
  a) If a function or method is tagged "TO-BE:" in the constraint card,
     it does NOT exist in the codebase yet.  You MUST describe the root
     cause as "the mechanism is absent / not yet implemented", NEVER as
     "already implemented but bypassed" or "dead code".
  b) Do NOT infer that a function exists because a requirement mentions it.
     Existence requires explicit evidence from Deep Search (a Read or Grep
     result showing the definition).
  c) Do NOT re-interpret a plain constant as a vestige of an abandoned
     dynamic-adjustment mechanism.  A constant is a constant unless Deep
     Search found accompanying dynamic logic.
  d) If your root-cause narrative contradicts a "TO-BE:" tag in the
     constraint card, stop, re-read the evidence, and correct the narrative
     before emitting the report.

═══════════════════════════════════════════════════════════
Other rules:
═══════════════════════════════════════════════════════════
- Allowed tools: Agent (deep-search sub-agent), TodoWrite,
  mcp__evidence__update_localization.
- Do NOT read files directly — delegate all file access to Deep Search.
- Keep your TodoWrite list up-to-date; mark tasks done as you go.
"""

# Deep Search sub-agent definition
_DEEP_SEARCH_AGENT_DEF = AgentDefinition(
    description=(
        "Deep search specialist. Use this agent to investigate a specific "
        "TODO task by searching the repository with Grep, Read, and Glob. "
        "It explores call chains, data flow, and similar patterns."
    ),
    prompt=DEEP_SEARCH_SYSTEM_PROMPT,
    tools=["Grep", "Read", "Glob", "TodoWrite"],
    model="sonnet",
)


async def _run_orchestrator_async(
    issue_id: str,
    artifacts_dir: str | Path,
    repo_dir: str | Path,
) -> str:
    """Run the full evidence-closure loop.

    Args:
        issue_id:      Identifier for the issue (used in prompts).
        artifacts_dir: Directory containing the .md artifact files.
        repo_dir:      Root of the repository to search.

    Returns:
        Final closure report (Markdown string).
    """
    artifacts_dir = Path(artifacts_dir)
    repo_dir = Path(repo_dir)

    # Step 1: Run parser standalone (MCP works reliably outside subagent context)
    artifact_text = load_artifacts(artifacts_dir)
    print("[orchestrator] Running parser agent...")
    evidence = await _run_parser_async(artifact_text)
    print("[orchestrator] Parser done.")

    output_dir = artifacts_dir.parent / "evidence"
    output_dir.mkdir(exist_ok=True)
    evidence_path = output_dir / "evidence_cards.json"
    evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    print(f"[orchestrator] Evidence cards saved → {evidence_path}")

    # Register absolute path so update_localization MCP tool can write back to it
    # regardless of what cwd the MCP server process uses.
    set_evidence_json_path(evidence_path.resolve())

    initial_prompt = (
        f"Issue ID: {issue_id}\n"
        f"Repository root: {repo_dir}\n\n"
        f"Initial EvidenceCards (parsed from artifacts):\n"
        f"```json\n{evidence.model_dump_json(indent=2)}\n```\n\n"
        "Begin the gap-filling evidence-closure loop now."
    )

    options = ClaudeAgentOptions(
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        allowed_tools=["Agent", "TodoWrite", "mcp__evidence__update_localization"],
        agents={
            "deep-search": _DEEP_SEARCH_AGENT_DEF,
        },
        mcp_servers={"evidence": evidence_server},
        cwd=str(repo_dir),
        permission_mode="acceptEdits",
        env=sdk_env(),
    )

    final_result = ""
    async for message in query(prompt=initial_prompt, options=options):
        if hasattr(message, "result"):
            final_result = message.result

    # --- Programmatic exact_code_regions fallback ---
    # If the LLM didn't call update_localization (or called it with empty regions),
    # parse the structured EXACT_LINES block from the closure report and write it
    # back to the JSON ourselves.  This is a hard guarantee, not a best-effort prompt.
    current_evidence = get_submitted_evidence()
    if current_evidence is not None and not current_evidence.localization.exact_code_regions:
        extracted = _parse_exact_lines_from_report(final_result)
        if extracted:
            from src.models.evidence import LocalizationCard
            loc = current_evidence.localization
            current_evidence.localization = LocalizationCard(
                suspect_entities=loc.suspect_entities,
                exact_code_regions=extracted,
                call_chain_context=loc.call_chain_context,
                dataflow_relevant_uses=loc.dataflow_relevant_uses,
            )
            evidence_path.resolve().write_text(
                current_evidence.model_dump_json(indent=2), encoding="utf-8"
            )
            print(
                f"[orchestrator] exact_code_regions back-filled from report "
                f"({len(extracted)} entries) → {evidence_path}"
            )
        else:
            print(
                "[orchestrator] WARNING: exact_code_regions still empty and no "
                "EXACT_LINES block found in closure report."
            )

    report_path = output_dir / "closure_report.md"
    report_path.write_text(final_result, encoding="utf-8")
    print(f"[orchestrator] Closure report saved → {report_path}")

    return final_result


def _parse_exact_lines_from_report(report: str) -> list[str]:
    """Extract exact line references from the closure report.

    Strategy 1 — structured block:
        ## EXACT_LINES (machine-readable)
        ```
        path/to/file.py:89
        path/to/file.py:126
        ```

    Strategy 2 — inline patterns (fallback):
        Scans the whole report for 'some/file.py:N' or 'some/file.py:N-M' tokens.

    Returns a deduplicated list of non-empty stripped strings, or [] if nothing found.
    """
    # Strategy 1: structured fenced block
    block_match = re.search(
        r"##\s+EXACT_LINES[^\n]*\n```[^\n]*\n(.*?)```",
        report,
        re.DOTALL | re.IGNORECASE,
    )
    if block_match:
        lines = [ln.strip() for ln in block_match.group(1).splitlines() if ln.strip()]
        if lines:
            return lines

    # Strategy 2: inline file:line tokens  (e.g. "face_detector.py:89" or "models/face_detector.py:89-95")
    inline = re.findall(
        r"[\w./\-]+\.py:\d+(?:-\d+)?",
        report,
    )
    seen: set[str] = set()
    result: list[str] = []
    for token in inline:
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def run_orchestrator(
    issue_id: str,
    artifacts_dir: str | Path,
    repo_dir: str | Path,
) -> str:
    """Synchronous entry-point for the orchestrator."""
    return asyncio.run(_run_orchestrator_async(issue_id, artifacts_dir, repo_dir))
