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
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
)

from src.agents.parser_agent import load_artifacts, _run_parser_async
from src.agents.deep_search_agent import DEEP_SEARCH_SYSTEM_PROMPT
from src.config import sdk_env
from src.tools.ingestion_tools import (
    get_submitted_evidence,
    set_evidence_json_path,
    update_localization,
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
   - Is call_chain_context still empty?  This is a MANDATORY field if we
     have a suspect function.  Dispatch a Deep Search TODO:
     "Find callers of <function> to build call chain."
     Do NOT proceed to closure with an empty call_chain_context when
     suspect_entities contains at least one function.
   - Is dataflow_relevant_uses empty?  If suspect function modifies a data
     structure, dispatch: "Trace Def-Use of <variable>."

3. CHECK CONSTRAINT GAPS
   - Are behavioral_constraints populated?
   - Does the document mention similar APIs but similar_implementation_patterns
     is empty?  → dispatch Deep Search to find existing similar implementations.

4. CHECK STRUCTURAL GAPS
   - Are must_co_edit_relations empty despite localization having suspects?
     This is a MANDATORY field when suspect_entities is non-empty.
     → dispatch: "Find callers / dependents of <entity> for co-edit analysis."
     Do NOT proceed to closure with empty must_co_edit_relations when we
     have confirmed defect locations.
   - Is dependency_propagation empty?
     → dispatch: "Trace interface / config dependencies of <module>."

5. READY FOR CLOSURE CHECK
   When ALL key fields in ALL four cards contain real data, AND you cannot
   formulate any more concrete, executable Deep Search tasks based on
   current card information, mark all Todos as done and proceed to closure.

   MANDATORY non-empty fields before closure (in addition to HARD RULES):
   - localization.suspect_entities (at least one file + one function)
   - localization.exact_code_regions (at least one entry)
   - localization.call_chain_context (at least one chain if a function is suspect)
   - structural.must_co_edit_relations (at least one entry if suspects exist)

   BOUNDARY RULE: Your job is to fill the cards with facts.  Do NOT try to
   judge "which information is harmful or irrelevant to the bug" — that is
   a downstream Closure Checker's responsibility.  You only ensure that the
   cards have evidence, not that the evidence solves the bug.

═══════════════════════════════════════════════════════════
PERSISTING FINDINGS — MANDATORY after every Deep Search return
═══════════════════════════════════════════════════════════

Immediately after reading each Deep Search report, you MUST parse ALL
structured sections and persist them via `mcp__evidence__update_localization`.
The tool now accepts ALL of these fields — use every one that applies:

a. Parse "EXACT_LINES" block → exact_code_regions
b. Parse "Call Chain Context" section → call_chain_context
   (each chain as an "A -> B -> C" string)
c. Parse "Dataflow Relevant Uses" section → dataflow_relevant_uses
d. Parse "Must Co-Edit Relations" section → must_co_edit_relations
e. Parse "Dependency Propagation" section → dependency_propagation
f. Parse "Missing Elements to Implement" section → missing_elements_to_implement
g. Extract confirmed suspect entities → suspect_entities

Call `mcp__evidence__update_localization` with ALL applicable fields in a
single call.  Do this BEFORE re-assessing closure — persisting is mandatory.

CRITICAL: If Deep Search reported call chains but you only pass
exact_code_regions to update_localization, the call chain data is LOST.
The JSON is the sole Source of Truth for downstream agents.  Any finding
that is not written to JSON does not exist.

═══════════════════════════════════════════════════════════
CLOSURE — JSON is the sole output, no Markdown report
═══════════════════════════════════════════════════════════

You are a pure evidence collector.  You do NOT produce any natural-language
analysis, summary, closure report, fix suggestion, or Markdown document.

When you determine that all mandatory fields are populated and no more
Deep Search tasks can be formulated, do the following:

1. Make one FINAL call to `mcp__evidence__update_localization` to ensure
   every piece of evidence you have is persisted to JSON.
2. Output ONLY a short declarative statement:
   "EVIDENCE_COLLECTION_COMPLETE — all cards populated."
3. Stop.  Do not write any prose, root-cause analysis, call-chain diagrams,
   or fix suggestions.  Those responsibilities belong to downstream agents
   that will read the JSON as their sole Source of Truth.

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
  evidence that the defect is located.  TO-BE items that are confirmed
  absent from the codebase must be written to
  constraint.missing_elements_to_implement via update_localization.

RULE 4 — FACT-ALIGNMENT in JSON fields: every entry written to the JSON
  evidence cards MUST be grounded in what Deep Search actually found, not
  inferred from requirements or conversation memory.

  a) If a function or method is tagged "TO-BE:" in the constraint card,
     it does NOT exist in the codebase yet.  Write it to
     missing_elements_to_implement, NEVER to suspect_entities or
     exact_code_regions.
      A method that exists as a definition but lacks callers is dead code.
      It belongs in suspect_entities, not missing_elements_to_implement.
      Do NOT write it to both fields.
  b) Do NOT infer that a function exists because a requirement mentions it.
     Existence requires explicit evidence from Deep Search (a Read or Grep
     result showing the definition).
  c) Do NOT write speculative entries to any JSON field.  Every entry must
     trace back to a concrete Deep Search finding.

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
        Path to the final evidence_cards.json file (as a string).
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

    initial_prompt_text = (
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
        mcp_servers={
            "evidence": create_sdk_mcp_server(
                name="evidence",
                version="1.0.0",
                tools=[update_localization],
            ),
        },
        cwd=str(repo_dir),
        permission_mode="acceptEdits",
        env=sdk_env(),
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(initial_prompt_text)
        async for message in client.receive_response():
            pass  # Evidence is persisted to JSON via update_localization MCP calls

    # --- Post-loop validation: warn about empty mandatory fields ---
    current_evidence = get_submitted_evidence()
    if current_evidence is not None:
        loc = current_evidence.localization
        struc = current_evidence.structural
        warnings: list[str] = []
        if not loc.exact_code_regions:
            warnings.append("exact_code_regions is empty")
        if not loc.call_chain_context:
            warnings.append("call_chain_context is empty")
        if not loc.suspect_entities:
            warnings.append("suspect_entities is empty")
        if not struc.must_co_edit_relations:
            warnings.append("must_co_edit_relations is empty")
        if warnings:
            for w in warnings:
                print(f"[orchestrator] WARNING: {w}")
        else:
            print("[orchestrator] All mandatory evidence fields populated.")

        # Re-save final state (in case the last update_localization call
        # was not the very last action)
        evidence_path.resolve().write_text(
            current_evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"[orchestrator] Final evidence JSON saved → {evidence_path}")
    else:
        print("[orchestrator] ERROR: No evidence cards in memory after loop.")

    return str(evidence_path)


def run_orchestrator(
    issue_id: str,
    artifacts_dir: str | Path,
    repo_dir: str | Path,
) -> str:
    """Synchronous entry-point for the orchestrator."""
    return asyncio.run(_run_orchestrator_async(issue_id, artifacts_dir, repo_dir))
