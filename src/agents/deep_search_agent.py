"""
Deep Search sub-agent (phase 18.E): given a specific TODO investigation task
and the current EvidenceCards snapshot, searches the repository using native
Grep / Read / Glob tools and returns a structured DeepSearchReport.

Phase 18.E adds a self-reflection round after the first structured output:
the agent verifies its own findings for hallucination and boundary issues
before returning the final report.
"""

import asyncio
import re
from pathlib import Path

from src.agents._structured import run_structured_query
from src.models.context import EvidenceCards
from src.models.report import DeepSearchReport


DEEP_SEARCH_SYSTEM_PROMPT = """\
You are a Deep Search Agent. The user message gives you exactly ONE
RequirementItem to verify against the current code using Grep/Read/Glob.

Your job, for that single requirement:
1. Locate the relevant code (call chain, data flow, callers, similar impls).
2. Decide a verdict among: AS_IS_COMPLIANT, AS_IS_VIOLATED, TO_BE_MISSING,
   TO_BE_PARTIAL.
3. Populate target_requirement_id, requirement_verdict, requirement_findings,
   requirement_evidence_locations. evidence_locations must be non-empty
   unless the verdict is AS_IS_COMPLIANT.
4. Also fill any AS-IS observations (localization.*, structural.*, similar
   implementation patterns) you uncovered along the way.

CRITICAL: requirement_evidence_locations MUST use format 'file.py:LINE' or
'file.py:LINE-LINE'. NEVER use bare file paths without line numbers.
- For files that don't exist yet, reference the integration points where they
  will be mounted or imported (e.g., 'src/routes/index.js:25').
- For whole-file references, use a line range (e.g., 'src/file.py:1-100').
- Every location must include a colon and line number(s).

Paths are relative to repo root. Do NOT modify files. Do NOT fabricate code
that isn't there.
"""


REFLECTION_SYSTEM_PROMPT = """\
You are a Deep Search Agent doing SELF-REFLECTION on your prior findings.

Your task: review the DeepSearchReport you just produced and self-correct
before returning the final report.

SELF-REFLECTION CHECKS:
1. TOKEN TRACEABILITY — For every backtick-enclosed snippet or function-name
   in your findings, verify: "Did my Read tool actually return content
   containing this token?"  Any snippet that is NOT in a file you Read
   during this investigation is a HALLUCINATION.  Delete or rephrase such
   tokens; do not assert code exists that you did not verify.

2. BOUNDARY ENUMERATION — If your verdict is AS_IS_VIOLATED, TO_BE_MISSING,
   or TO_BE_PARTIAL AND your findings contain a prescriptive fix
   ("correct is X", "should be Y", "must use Z"), enumerate at least 2
   edge cases for the requirement's behaviour:
     - null / undefined / zero vs non-null / defined / non-zero
     - empty collection vs non-empty collection
     - boundary values (e.g. exactly at max, just over max)
   Substitute your prescriptive fix into each case.  If any case fails
   the requirement description, record this as an open issue.

   IMPORTANT (Phase 22): Write boundary enumeration results to the
   `boundary_analysis` field, NOT in `requirement_findings`. The findings
   field should contain ONLY verified code defects and observations from
   actual code. Keep hypothetical edge case speculation, "OPEN ISSUE" notes,
   and "what if X is undefined" analysis in boundary_analysis. This separation
   ensures closure-checker validates actual defects, not hypothetical risks.

3. VERDICT CONSISTENCY — If findings mention overlapping code with other
   requirements, ensure your verdict is consistent with the code's
   actual behaviour in those shared regions.

If reflection reveals issues, revise the DeepSearchReport fields accordingly.
Return a DeepSearchReport (original or revised).  Do NOT fabricate new
Read results — stay within what your previous investigation opened.
"""


_BACKTICK_RE = re.compile(r"`([^`]+)`")


async def _run_deep_search_async(
    todo_task: str,
    evidence: EvidenceCards,
    repo_dir: Path | None = None,
) -> DeepSearchReport:
    evidence_summary = evidence.model_dump_json(indent=2)
    prompt = (
        f"TODO task: {todo_task}\n\n"
        f"Current evidence cards:\n```json\n{evidence_summary}\n```\n\n"
        "Investigate and return a structured report of your findings."
    )

    # ── Phase 18.E: Round 1 — primary investigation ──────────────────
    report = await run_structured_query(
        system_prompt=DEEP_SEARCH_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=DeepSearchReport,
        component="deep-search",
        allowed_tools=["Grep", "Read", "Glob", "TodoWrite"],
        max_turns=30,
        max_budget_usd=1.0,
        cwd=str(repo_dir) if repo_dir is not None else None,
    )

    # ── Phase 18.E: Round 2 — self-reflection ────────────────────────
    report_json = report.model_dump_json(indent=2)
    reflection_prompt = (
        "Review and self-correct your findings:\n\n"
        f"## Your DeepSearchReport\n"
        f"```json\n{report_json}\n```\n\n"
        "Execute the self-reflection checks described in the system prompt. "
        "Return a DeepSearchReport (original or revised)."
    )

    try:
        reflected = await run_structured_query(
            system_prompt=REFLECTION_SYSTEM_PROMPT,
            user_prompt=reflection_prompt,
            response_model=DeepSearchReport,
            component="deep-search-reflection",
            allowed_tools=["Grep", "Read", "Glob"],
            max_turns=10,
            max_budget_usd=0.5,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
        # Use reflected report if it came back with valid data
        if reflected and reflected.target_requirement_id:
            return reflected
    except Exception as exc:
        print(
            f"[deep-search] reflection round failed ({type(exc).__name__}), "
            f"using first-round report",
            flush=True,
        )

    return report


def run_deep_search(
    todo_task: str,
    evidence: EvidenceCards,
    repo_dir: Path | None = None,
) -> DeepSearchReport:
    """Synchronous wrapper.

    Args:
        todo_task: A specific investigation task string from the orchestrator.
        evidence:  Current EvidenceCards state.

    Returns:
        DeepSearchReport with structured findings.
    """
    return asyncio.run(_run_deep_search_async(todo_task, evidence, repo_dir))
