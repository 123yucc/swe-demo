"""
Closure Checker sub-agent — evidence-closure questioner (phase 25).

Re-scoped from the phase-18 manifest auditor. The two FACTUAL checks
(verdict_vs_code, findings_anti_hallucination) were lowered into the
deterministic code grounding gate (src/orchestrator/grounding.py +
ast_grounding.py), which runs BEFORE this agent. The closure-checker now owns
the two SEMANTIC dimensions of a valid evidence closure:

  ① Sufficiency  — can a repair commit be made from this evidence, or is the
                   localization / constraint coverage still too thin?
  ② Consistency  — do the active requirement verdicts, the compliant group, and
                   the findings agree, or do they contradict each other?

The one per-task LLM check that survives is ``prescriptive_boundary_self_check``
(a semantic judgement on whether a prescriptive fix survives edge cases),
carried via the AuditManifest as before.
"""

import asyncio
from pathlib import Path

from src.agents._structured import run_structured_query
from src.models.audit import AuditManifest
from src.models.context import EvidenceCards
from src.models.verdict import ClosureVerdict


CLOSURE_CHECKER_SYSTEM_PROMPT = """\
You are a Closure Checker — an evidence-closure QUESTIONER.

A valid evidence closure must satisfy three dimensions. TWO are already enforced
by deterministic code gates that ran BEFORE you and that you must NOT re-check:
  - Sufficiency (FORM): every requirement has a non-UNCHECKED verdict.
  - Correct attribution (GROUNDING): every cited code region / symbol / findings
    snippet was mechanically verified to exist in the repository.

YOUR job is the two SEMANTIC dimensions the code gates cannot judge:

────────────────────────────────────────────────────────────────────────
① SUFFICIENCY (semantic)
────────────────────────────────────────────────────────────────────────
Ask, for the WHOLE evidence set: "Could an engineer open this and make ONE
correct repair commit right now?" FAIL the sufficiency dimension when, e.g.:
  - a non-compliant requirement's repair_targets do not land on any concrete
    code location (no file/line to edit),
  - a constraint that the fix must honour is stated but not actionable,
  - a key piece of information is still unknown (the findings hedge or defer).
When you FAIL sufficiency, name the single most-blocking requirement (or none)
and pick the conflicting_field that best localizes the gap.

────────────────────────────────────────────────────────────────────────
② CONSISTENCY
────────────────────────────────────────────────────────────────────────
Cross-check the ACTIVE requirements AND the COMPLIANT GROUP (provided in a
separate section below) AND the findings against each other. FAIL the
consistency dimension when, e.g.:
  - two requirements over the same code reach incompatible verdicts (one says
    AS_IS_VIOLATED, another over the same region is AS_IS_COMPLIANT),
  - a compliant-group entry asserts behavior that an active requirement's
    findings directly contradict,
  - co-edit relations imply a change that some verdict denies is needed,
  - SPEC-vs-FINDINGS CONTRADICTION: a requirement has a non-compliant verdict
    (AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL) yet its findings argue the
    cited code "must remain unchanged" / "is load-bearing" / "must stay as-is",
    OR defer the fix to another requirement ("this is a side-effect of fixing
    req-X", "becomes dead code once req-Y lands"). A non-compliant verdict
    means a change is owed at the cited location; findings that argue against
    making any change there, or that offload the change point onto a different
    requirement, are internally inconsistent. FAIL with conflicting_field
    "findings".
When you FAIL consistency, list ALL implicated requirement_ids and set
conflicting_field to "<cross-req>" (or the specific field in conflict).

A separate "Dynamic Reachability Notes" section may report that the bug's
symptom was reproduced at runtime but the observed failure path did NOT
traverse a requirement's cited location. Treat this as a SOFT consistency
input only: it can corroborate a suspicion that an attribution is off, but a
reproduction script is legitimately incomplete, so it must NOT be the sole
basis for a FAIL. Weigh it together with the static evidence.

────────────────────────────────────────────────────────────────────────
PER-TASK CHECK (from the AuditManifest)
────────────────────────────────────────────────────────────────────────
prescriptive_boundary_self_check
  The findings may contain prescriptive language ("correct is X", "must use Y").
  Enumerate at least 2 edge cases for the requirement's behavior, substitute the
  prescriptive fix, and check all results satisfy the requirement's description.
  ESCAPE HATCH: if a failing edge case requires a SEPARATE feature not in the
  requirement → PASS with a caveat in the explanation. If the edge case
  contradicts the prescriptive fix itself → FAIL.

You have read-only repo access via Grep, Read, Glob. Use it to substantiate
your sufficiency/consistency reasoning when needed.

Lines starting with ``anchor:`` in the manifest warnings are consistency
anchors already machine-verified by a code gate — do not re-check them.

────────────────────────────────────────────────────────────────────────
OUTPUT — ClosureVerdict
────────────────────────────────────────────────────────────────────────
  * dimension_findings: one DimensionFinding per dimension you judged
    (sufficiency and consistency). Each has:
      - dimension: "sufficiency" | "consistency"
      - status: "PASS" | "FAIL"
      - requirement_ids: ids implicated (MUST exist in the input evidence)
      - conflicting_field: choose EXACTLY ONE of this fixed enum, or null for
        PASS: {"verdict", "findings", "evidence_locations", "repair_targets",
        "missing_elements", "<cross-req>"}. Do NOT invent field names.
      - explanation: one sentence (written into rework feedback)
  * audited: one AuditResult per AuditManifest task (only
    prescriptive_boundary_self_check). Omit no task.
  * verdict = EVIDENCE_MISSING if ANY dimension_finding is FAIL or ANY audited
    check is FAIL; otherwise CLOSURE_APPROVED.
  * rationale: 1-2 sentences. For EVIDENCE_MISSING, summarize the biggest gap.
  * missing: one line per FAIL naming the requirement id(s) and the dimension.
  * suggested_tasks: requirement ids that need deep-search rework.

Do NOT fabricate code. Do NOT return CLOSURE_APPROVED if any dimension or task
check failed.
"""


def _format_compliant_group(evidence: EvidenceCards) -> str:
    """Render the compliant group (requirement_status) for consistency auditing.

    The compliant group lives in ``EvidenceCards.requirement_status`` and is
    intentionally excluded from ``format_for_prompt`` / the active evidence
    JSON. The consistency dimension needs it, so we inject it explicitly here
    (phase 25 schema-breakage fix).
    """
    statuses = evidence.requirement_status
    if not statuses:
        return "(no compliant requirements recorded)"
    lines: list[str] = []
    for s in statuses:
        locs = ", ".join(s.evidence_locations) if s.evidence_locations else "(none)"
        lines.append(
            f"- {s.id} [{s.origin}] AS_IS_COMPLIANT\n"
            f"  text: {s.text}\n"
            f"  reason: {s.short_reason or '(none)'}\n"
            f"  locations: {locs}"
        )
    return "\n".join(lines)


def _format_dynamic_notes(notes: list[str] | None) -> str:
    """Render phase-26 dynamic reachability notes for consistency auditing.

    These come from a single runtime reproduction of the bug on base_commit.
    Each ``dynamic_not_reached`` note means: the bug's symptom WAS reproduced,
    but the observed failure path did NOT traverse a cited location — a SOFT
    signal that the cited attribution may be off. They are advisory only; the
    repro can legitimately be incomplete, so they never force a verdict. Empty
    when dynamic grounding produced no actionable not-reached note (no signal,
    unverifiable, or every cited location was on the failure path).
    """
    if not notes:
        return "(no dynamic reachability signal — runtime grounding was unverifiable or confirmatory)"
    return "\n".join(f"- {n}" for n in notes)


async def _run_closure_checker_async(
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None,
    dynamic_notes: list[str] | None = None,
) -> ClosureVerdict:
    evidence_json = evidence.model_dump_json(indent=2, exclude={"requirement_status"})
    manifest_json = manifest.model_dump_json(indent=2)
    compliant_block = _format_compliant_group(evidence)
    dynamic_block = _format_dynamic_notes(dynamic_notes)
    prompt = (
        "## Audit Manifest (prescriptive checks + warnings)\n"
        f"```json\n{manifest_json}\n```\n\n"
        "## Active Evidence Cards (requirements under repair)\n"
        f"```json\n{evidence_json}\n```\n\n"
        "## Compliant Group (verified AS_IS_COMPLIANT — for CONSISTENCY auditing)\n"
        f"{compliant_block}\n\n"
        "## Dynamic Reachability Notes (runtime reproduction — SOFT consistency input)\n"
        f"{dynamic_block}\n\n"
        "Judge the SUFFICIENCY and CONSISTENCY dimensions over ALL of the above "
        "(active requirements AND the compliant group). Execute each AuditTask's "
        "prescriptive_boundary_self_check. Return a ClosureVerdict with one "
        "DimensionFinding per dimension and one AuditResult per task."
    )

    return await run_structured_query(
        system_prompt=CLOSURE_CHECKER_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=ClosureVerdict,
        component="closure-checker",
        allowed_tools=["Grep", "Read", "Glob", "TodoWrite"],
        max_turns=30,
        max_budget_usd=2.5,
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
        ClosureVerdict with dimension_findings + per-task AuditResults.
    """
    return asyncio.run(_run_closure_checker_async(evidence, manifest, repo_dir))
