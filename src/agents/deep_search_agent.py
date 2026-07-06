"""Deep Search sub-agent (phase 18.E): given a specific TODO investigation task
and the current EvidenceCards snapshot, searches the repository using native
Grep / Read / Glob tools and may also actively query long-term memory using
the MemGovern-style Search → Browse workflow before returning a structured
DeepSearchReport.

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
   TO_BE_PARTIAL. You MUST pick one of these four — UNCHECKED is NEVER valid.
   If you cannot fully determine the status after investigation, use
   TO_BE_PARTIAL (meaning partial evidence exists but full verification is
   inconclusive).
3. Populate target_requirement_id, requirement_verdict, requirement_findings,
   requirement_evidence_locations. evidence_locations must be non-empty for
   AS_IS_COMPLIANT, AS_IS_VIOLATED, and TO_BE_PARTIAL. TO_BE_MISSING may be
   empty when the relevant definition is entirely absent.
4. Also fill any AS-IS observations (localization.*, structural.*, similar
   implementation patterns) you uncovered along the way.
5. When investigating EXISTING code (verdict AS_IS_VIOLATED or AS_IS_COMPLIANT),
   populate these constraint fields if you observe them:
   - semantic_boundaries: what the current code handles vs does not handle
     (input ranges, preconditions, invariants, edge cases it ignores).
   - behavioral_constraints: ordering requirements, thread-safety assumptions,
     side-effect rules, or implicit contracts callers depend on.
   - backward_compatibility: APIs, signatures, or behaviors that existing
     callers rely on and that must not change.
   Leave these empty for TO_BE_MISSING/TO_BE_PARTIAL (no existing code to observe).

AS_IS_COMPLIANT is a lightweight coverage status, not patch-planning material.
Only use it when the code was directly verified and the findings contain no
traceability failure, unverifiable, must be verified, not backed by Read, or
cannot be confirmed language. Otherwise use TO_BE_PARTIAL.

CRITICAL: requirement_evidence_locations MUST use format 'file.py:LINE' or
'file.py:LINE-LINE'. NEVER use bare file paths without line numbers.
- For files that don't exist yet, reference the integration points where they
  will be mounted or imported (e.g., 'src/routes/index.js:25').
- For whole-file references, use a line range (e.g., 'src/file.py:1-100').
- Every location must include a colon and line number(s).

Paths are relative to repo root. Do NOT modify files. Do NOT fabricate code
that isn't there.

────────────────────────────────────────────────────────────────────────
CONSISTENCY ANCHORS (structural.consistency_anchors)
────────────────────────────────────────────────────────────────────────

When the requirement matches ANY of the patterns below, you MUST populate
``structural.consistency_anchors`` with one entry per pair of code points
that must remain jointly consistent. A code gate will mechanically verify
each anchor's two endpoints; missing or unresolvable endpoints bounce the
pipeline back to deep-search.

Pattern A — Configuration ↔ code-side definition:
  The requirement (or the code) introduces a configuration/data file
  (yml/yaml/json/toml/ini/...) that references an identifier-shaped name
  (a class, enum, function, or method name). The code side must define
  that name. Emit one anchor per such reference.

Pattern B — Renamed or visibility-changed symbol:
  The requirement implies an exported symbol changes name, case, or
  visibility (public → private, etc.). Use Grep to enumerate every
  agent-visible reference to the OLD name across the repo, INCLUDING
  same-package ``_test.*`` files that ship at base_commit. Emit one
  anchor per old-vs-new reference pair so the gate can verify the rename
  is fully propagated. (Do NOT reference evaluator-injected hidden test
  fixtures — they are agent-invisible and out of scope.)

Pattern C — New file with mount/import sites elsewhere:
  The requirement implies a new file is added that other files must
  register, import, or mount. Emit one anchor per (new_file, mount_site)
  pair so the gate can verify both sides exist.

Anchor format (strict):
  '<path_a>:<locator_a> <-> <path_b>:<locator_b>'

Each locator is one of:
  * 'LINE'           — single line number
  * 'LINE-LINE'      — inclusive line range
  * 'class:NAME'     — symbol with prefix (also: func, method, type,
                       enum, field, name, key, interface, struct, var, const)
  * 'NAME'           — bare identifier (the gate verifies via word-boundary
                       grep in the file)

Examples (illustrative shape, NOT specific to any repo):
  - 'config/settings.yml:type=Foo <-> src/types.py:class Foo'
  - 'pkg/api.go:Bar <-> pkg/api_test.go:42'
  - 'src/new_module.py:1-10 <-> src/main.py:import:new_module'

If the requirement does NOT match Pattern A/B/C, leave consistency_anchors
empty. Do NOT pad with anchors that are not load-bearing.
"""


REFLECTION_SYSTEM_PROMPT = """\
You are a Deep Search Agent doing SELF-REFLECTION on your prior findings.

Your task: review the DeepSearchReport you just produced and self-correct
before returning the final report.

SELF-REFLECTION CHECKS:
1. TOKEN TRACEABILITY — verify against the REPOSITORY, not against your memory.
   CRITICAL FRAMING: You are a FRESH session. You have NOT read any file yet
   this round, and the working-memory "Retrieved Code Cache" / action history
   do NOT record the file reads from your investigation round. Therefore the
   ABSENCE of a Read in your history or an empty code cache is NEVER evidence
   of hallucination — treating it as such wrongly deletes real, verified
   findings. Do not reason from "I don't see that I read this."
   Instead, GROUND-TRUTH each claim against the repo using your Read/Grep/Glob
   tools NOW: for every cited evidence_location and every backtick-enclosed
   snippet or symbol name in your findings, open that exact file:line (or grep
   the symbol) in the repository and check whether the token is actually there.
     - If Read/Grep confirms the token exists at (or near) the cited location:
       KEEP the location and the finding.
     - Only if Read/Grep shows the file or line genuinely does NOT contain the
       cited token (or the file does not exist) is it a HALLUCINATION — then
       delete or correct that specific token/location.
   NEVER empty a non-empty evidence_locations list except for the specific
   locations you actively DISPROVED by reading the repo this round. A verdict
   of AS_IS_VIOLATED / AS_IS_COMPLIANT / TO_BE_PARTIAL with verified locations
   must retain those locations.

2. BOUNDARY ENUMERATION — If your verdict is AS_IS_VIOLATED, TO_BE_MISSING,
   or TO_BE_PARTIAL AND your findings contain a prescriptive fix
   ("correct is X", "should be Y", "must use Z"), enumerate at least 2
   edge cases for the requirement's behaviour:
     - null / undefined / zero vs non-null / defined / non-zero
     - empty collection vs non-empty collection
     - boundary values (e.g. exactly at max, just over max)
   Substitute your prescriptive fix into each case. If any case fails
   the requirement description, remove the prescription or use TO_BE_PARTIAL.

   Use this only to decide whether your prescriptive statement is safe.
   Do NOT write hypothetical edge-case speculation, "OPEN ISSUE" notes, or
   unverified concerns into requirement_findings. If the prescription is not
   fully supported by verified code evidence, remove it or downgrade the
   verdict to TO_BE_PARTIAL.

2b. SPEC PRIORITY — this is the REVERSE of check 2 and is just as important.
   Check 2 stops you from over-prescribing; this stops you from under-acting
   on an explicit instruction. When the requirement TEXT prescribes a change
   — it contains MUST / "must take precedence" / "should be" / "is required
   to" / a named behaviour the code is supposed to have — and your verdict is
   non-compliant (AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL), then:
     - Your findings MUST NOT argue that the cited code "should remain
       unchanged", "is load-bearing", or "must stay as-is". A non-compliant
       verdict means a change is owed AT the cited location. Claiming the
       location both violates the requirement AND must not change is an
       internal contradiction — resolve it, do not ship it.
     - Your findings MUST NOT declare the fix to be "a side-effect of fixing
       req-X", "dead code once req-Y lands", or otherwise defer this
       requirement's change point to another requirement. Each requirement is
       repaired on its own cited location, in a world where nothing else
       changed. Reasoning of the form "this becomes unreachable once req-X is
       fixed" is a cross-requirement coupling violation: fix it here.
     - If you genuinely believe the requirement itself is wrong or the code is
       already correct, that is a VERDICT decision (use AS_IS_COMPLIANT and say
       why), NOT a license to keep a non-compliant verdict while arguing in the
       findings against making any change. The verdict and the findings must
       agree on whether a change is owed.

3. VERDICT CONSISTENCY — If findings mention overlapping code with other
   requirements, ensure your verdict is consistent with the code's
   actual behaviour in those shared regions. Reasoning of the form "this
   becomes dead code once req-X is fixed" is a consistency violation: do not
   let one requirement's fix silently absorb another requirement's cited
   change point.

4. ANCHOR ENDPOINT VERIFICATION — For every entry you wrote in
   ``structural.consistency_anchors``, both LHS and RHS endpoints must
   resolve in files you actually Read during this investigation. If only
   one endpoint was verified, either drop the anchor or move the half-
   verified observation into ``findings`` as a single-side note. Do not
   leave anchors whose existence relies on a file you never opened.

If reflection reveals issues, revise the DeepSearchReport fields accordingly.
Return a DeepSearchReport (original or revised).  Do NOT fabricate new
Read results — stay within what your previous investigation opened.
"""


_BACKTICK_RE = re.compile(r"`([^`]+)`")


async def _run_deep_search_async(
    todo_task: str,
    evidence: EvidenceCards,
    repo_dir: Path | None = None,
    working_memory_block: str = "",
) -> DeepSearchReport:
    evidence_summary = evidence.model_dump_json(indent=2)
    memory_section = (
        f"{working_memory_block.strip()}\n\n"
        if working_memory_block and working_memory_block.strip()
        else ""
    )
    prompt = (
        f"{memory_section}"
        f"TODO task: {todo_task}\n\n"
        f"Current evidence cards:\n```json\n{evidence_summary}\n```\n\n"
        "Investigate and return a structured report of your findings. "
        "When useful, use long-term memory progressively: first search for "
        "relevant experience summaries, then browse only the most relevant "
        "experience ids for detailed fix guidance, and re-search with refined "
        "keywords if the first results are not sufficient."
    )

    # ── Phase 18.E: Round 1 — primary investigation ──────────────────
    report = await run_structured_query(
        system_prompt=DEEP_SEARCH_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=DeepSearchReport,
        component="deep-search",
        allowed_tools=["Grep", "Read", "Glob", "TodoWrite"],
        max_turns=50,
        max_budget_usd=3.0,
        cwd=str(repo_dir) if repo_dir is not None else None,
    )

    # ── Phase 18.E: Round 2 — self-reflection ────────────────────────
    report_json = report.model_dump_json(indent=2)
    reflection_prompt = (
        f"{memory_section}"
        "Review and self-correct your findings:\n\n"
        f"## Your DeepSearchReport\n"
        f"```json\n{report_json}\n```\n\n"
        "Execute the self-reflection checks described in the system prompt. "
        "IMPORTANT: You MUST preserve the requirement_verdict and "
        "target_requirement_id from the original report unless your "
        "reflection explicitly justifies changing the verdict. "
        "Return a DeepSearchReport (original or revised)."
    )

    try:
        reflected = await run_structured_query(
            system_prompt=REFLECTION_SYSTEM_PROMPT,
            user_prompt=reflection_prompt,
            response_model=DeepSearchReport,
            component="deep-search-reflection",
            allowed_tools=["Grep", "Read", "Glob"],
            max_turns=25,
            max_budget_usd=1.5,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
        # Use reflected report only if it preserved the verdict.
        # The reflection round's job is to correct findings/anchors, not
        # to downgrade the verdict. If it returned the schema default
        # (TO_BE_PARTIAL) while Round 1 had a more specific verdict,
        # prefer Round 1's verdict.
        if reflected and reflected.target_requirement_id:
            # Carry over Round 1 verdict if reflection didn't set one
            # that differs meaningfully (i.e., not just the schema default
            # when Round 1 had something specific).
            r1_verdict = report.requirement_verdict
            r2_verdict = reflected.requirement_verdict
            if r2_verdict == "TO_BE_PARTIAL" and r1_verdict != "TO_BE_PARTIAL":
                reflected.requirement_verdict = r1_verdict
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
    working_memory_block: str = "",
) -> DeepSearchReport:
    """Synchronous wrapper.

    Args:
        todo_task: A specific investigation task string from the orchestrator.
        evidence:  Current EvidenceCards state.
        working_memory_block: Optional rendered SharedWorkingMemory section
            (LTM summaries, custom repair discipline, build feedback) to
            inject ahead of the TODO. Empty string disables injection.

    Returns:
        DeepSearchReport with structured findings.
    """
    return asyncio.run(
        _run_deep_search_async(
            todo_task, evidence, repo_dir, working_memory_block
        )
    )
