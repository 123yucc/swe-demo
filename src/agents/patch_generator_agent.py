"""
Patch Generator sub-agent: reads the PatchPlan from SharedWorkingMemory,
reads the target source files, and produces SEARCH/REPLACE edits that are
applied via the apply_search_replace MCP tool.

This agent is invoked directly by the orchestrator (code-driven pipeline)
rather than via the Agent tool dispatch.
"""

import asyncio
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)

from src.config import sdk_env, sdk_model_options, sdk_stderr_logger
from src.models.memory import SharedWorkingMemory
from src.tools.patch_tools import apply_search_replace

PATCH_GENERATOR_SYSTEM_PROMPT = """\
You are a Patch Generator — a precise code editor that executes a PatchPlan.

You receive a PatchPlan with preserved_findings and the original evidence
requirements. Produce SEARCH/REPLACE edits.

For each FileEditPlan in order:
1. READ the target file before generating any SEARCH blocks
2. IDENTIFY exact code regions that need to change
3. CONSTRUCT SEARCH/REPLACE blocks:
   <<<<<<SEARCH
   [exact old code to find]
   ======SPLIT
   [new code to replace it with]
   >>>>>>REPLACE
4. CALL mcp__patch__apply_search_replace for each file
5. If ERROR: re-read the file, adjust, and retry

CRITICAL — preserved_findings hard constraints (phase 18.D):
The preserved_findings list contains verbatim prescriptive snippets from the
original evidence findings.  These are HARD CONSTRAINTS — your SEARCH/REPLACE
must match the exact code expressions shown.  Before submitting each edit,
verify that it satisfies every preserved_findings snippet for this file.

If a preserved_findings snippet appears to conflict with your edit, DO NOT
ignore it — re-read the file and adjust the implementation to satisfy the
constraint.  Preserved findings are authoritative over your own inference.

Examples of preserved_findings hard constraints:
- "`ttl || Date.now() + interval > max`" → the formula must appear exactly
- "correct comparison: (ttl || Date.now()) + interval > max" → use this formula

Rules:
- SEARCH text MUST be exact verbatim copy of current file content
- MINIMAL DIFF: change only what the plan requires
- Preserve existing indentation style
- Apply edits in dependency order
- preserved_findings are hard constraints: verify before submitting

After all files are patched, output: PATCH_APPLIED
If any file could not be patched: PATCH_INCOMPLETE
"""


async def _run_patch_generator_async(
    memory: SharedWorkingMemory,
    repo_dir: Path,
) -> bool:
    """Run the patch generator agent. Returns True if all patches applied."""
    # ── Phase 18.D: Include original requirements text in prompt ───────
    req_section = ""
    if memory.evidence_cards and memory.evidence_cards.requirements:
        req_lines = []
        for req in memory.evidence_cards.requirements:
            if req.verdict not in ("AS_IS_COMPLIANT", "UNCHECKED"):
                req_lines.append(
                    f"### {req.id} ({req.origin})\n"
                    f"{req.text}\n"
                    f"verdict: {req.verdict}\n"
                    f"findings: {req.findings}"
                )
        if req_lines:
            req_section = "\n\n## Relevant Requirements (verbatim)\n" + "\n\n".join(req_lines)

    prompt = (
        "Execute the following patch plan:\n\n"
        f"{memory.format_for_prompt()}\n"
        f"{req_section}\n\n"
        "Apply SEARCH/REPLACE edits for each file in the plan. "
        "Respect preserved_findings as hard constraints."
    )

    patch_mcp = create_sdk_mcp_server(
        name="patch",
        version="1.0.0",
        tools=[apply_search_replace],
    )

    options = ClaudeAgentOptions(
        system_prompt=PATCH_GENERATOR_SYSTEM_PROMPT,
        allowed_tools=["Read", "mcp__patch__apply_search_replace", "TodoWrite"],
        mcp_servers={"patch": patch_mcp},
        cwd=str(repo_dir),
        max_turns=40,
        max_budget_usd=1.5,
        **sdk_model_options(),
        permission_mode="acceptEdits",
        max_thinking_tokens=0,
        stderr=sdk_stderr_logger("patch-generator"),
        env=sdk_env(),
    )

    result_text = ""
    limit_hit: str | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                if message.subtype in ("error_max_turns", "error_max_budget_usd"):
                    limit_hit = message.subtype

    if limit_hit is not None:
        print(
            f"[patch-generator] aborted due to per-query limit: {limit_hit}",
            flush=True,
        )
        return False

    # Check outcome
    if "PATCH_APPLIED" in result_text:
        return True
    # Even without explicit PATCH_APPLIED, check if edits were made
    if "PATCH_INCOMPLETE" in result_text:
        return False
    # If no explicit marker, assume partial success
    return "ERROR" not in result_text


def run_patch_generator(memory: SharedWorkingMemory, repo_dir: Path) -> bool:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with patch plan and cached code.
        repo_dir: Repository root directory.

    Returns:
        True if patches were successfully applied.
    """
    return asyncio.run(_run_patch_generator_async(memory, repo_dir))
