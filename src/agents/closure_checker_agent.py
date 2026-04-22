"""
Closure Checker sub-agent — manifest-driven audit gate (phase 18.B/C).

The orchestrator pre-computes an AuditManifest (src/orchestrator/audit.py)
with deterministic scope and per-requirement check types.  The closure-checker
executes exactly the tasks in the manifest — it does not decide what to audit.

Three semantic check types (18.C):
  verdict_vs_code        — does the cited code actually support the verdict?
  findings_anti_hallucination — are backtick code snippets in findings verified?
  prescriptive_boundary_self_check — does the prescriptive fix survive edge cases?

Structured output is produced via src/agents/_structured.py.
"""

import asyncio
from pathlib import Path

from src.agents._structured import run_structured_query
from src.models.audit import AuditManifest
from src.models.context import EvidenceCards
from src.models.verdict import ClosureVerdict


CLOSURE_CHECKER_SYSTEM_PROMPT = """\
You are a Closure Checker — a code-reviewer-style auditor.

You receive an AuditManifest listing exactly which requirements to audit and
which semantic checks to perform.  You do NOT decide what to audit; you
execute the checks in the manifest.

SUFFICIENCY and CORRECT ATTRIBUTION (format of evidence_locations) are
already enforced by code gates before you are called — do NOT re-check them.

You have read-only access to the repository via Grep, Read, and Glob.

────────────────────────────────────────────────────────────────────────
CHECK TYPES
────────────────────────────────────────────────────────────────────────

1. verdict_vs_code
   For each cited evidence_location, use Read to open the file region.
   Decide whether the code's actual behaviour supports the requirement's
   verdict (AS_IS_COMPLIANT / AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL).
   If the code contradicts the verdict → FAIL, explain the mismatch.
   If the code supports the verdict → PASS.

2. findings_anti_hallucination
   The findings field may contain backtick-enclosed code snippets (e.g.
   `db.mget`) that assert "this code exists".  For each backtick snippet,
   use Grep or Read to verify it actually exists in the cited file regions.
   If a snippet cannot be found in the Read output → FAIL, name the missing
   snippet and the file that was searched.
   If all snippets verified → PASS.

3. prescriptive_boundary_self_check
   The findings field may contain prescriptive language ("correct is X",
   "should be Y", "must use Z") that proposes a fix.  Before accepting the
   verdict, enumerate at least 2 edge cases for the requirement's behaviour
   (e.g. null vs non-null input, empty vs non-empty set, boundary values).
   Substitute the prescriptive fix into each edge case and check whether
   all results satisfy the requirement's original description.
   If any edge case violates the requirement → FAIL, describe the edge case
   and why the prescriptive fix fails it.
   If all edge cases pass → PASS.

────────────────────────────────────────────────────────────────────────
STRUCTURAL WARNINGS
────────────────────────────────────────────────────────────────────────

If the manifest includes warnings (from structural invariant checks I1/I3),
note them in your reasoning but do NOT fail on them — they are informational.

────────────────────────────────────────────────────────────────────────
OUTPUT
────────────────────────────────────────────────────────────────────────

Return a ClosureVerdict with:
  * verdict = CLOSURE_APPROVED if ALL audit tasks have only PASS (no FAIL).
  * verdict = EVIDENCE_MISSING if ANY audit task has a FAIL.
  * audited = one AuditResult per task in the manifest.  Omit no tasks.
  * missing = one line per FAIL, naming the requirement id and which check.
  * suggested_tasks = requirement ids that need deep-search rework.

Do NOT fabricate code.  Do NOT skip tasks.  Do NOT return CLOSURE_APPROVED
if any check failed.
"""


async def _run_closure_checker_async(
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None,
) -> ClosureVerdict:
    evidence_json = evidence.model_dump_json(indent=2)
    manifest_json = manifest.model_dump_json(indent=2)
    prompt = (
        "## Audit Manifest\n"
        f"```json\n{manifest_json}\n```\n\n"
        "## Evidence Cards (current state)\n"
        f"```json\n{evidence_json}\n```\n\n"
        "Execute every AuditTask in the manifest.  For each task, open the "
        "cited evidence_locations with Read, perform the required checks, "
        "and return one AuditResult per task.  Return a ClosureVerdict."
    )

    return await run_structured_query(
        system_prompt=CLOSURE_CHECKER_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=ClosureVerdict,
        component="closure-checker",
        allowed_tools=["Grep", "Read", "Glob", "TodoWrite"],
        max_turns=30,
        max_budget_usd=2.5,
        max_validation_retries=1,
        cwd=str(repo_dir) if repo_dir is not None else None,
    )


def run_closure_checker(
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None,
) -> ClosureVerdict:
    """Synchronous wrapper.

    Args:
        evidence: Current EvidenceCards state.
        manifest: Pre-computed AuditManifest from build_audit_manifest().
        repo_dir: Repository root path for Grep/Read/Glob.

    Returns:
        ClosureVerdict with per-task AuditResults.
    """
    return asyncio.run(_run_closure_checker_async(evidence, manifest, repo_dir))
