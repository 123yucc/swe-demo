"""
Orchestrator: drives the full repair pipeline via a code-driven while loop.

The pipeline is a state machine (see states.py) where LLM is only invoked
at semantic decision points — deep-search investigation, closure evaluation,
patch planning, and patch generation.  All flow control (state transitions,
allowed actions, iteration budgets) is enforced by code.

Previous architecture relied on a single LLM agent to manage the entire
state machine via prompt instructions and three post-hoc safety nets.
This version replaced that with:
  - PipelineState enum + transition table (states.py)
  - Code while-loop driving the pipeline
  - Direct function calls to sub-agents (no Agent tool dispatch)
  - Mechanical pre-checks before LLM calls (guards.py)
  - Structured output for deep-search and closure-checker
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_REQ_ID_RE = re.compile(r"req-\d{3,}")


def _extract_req_ids(text_parts: list[str]) -> list[str]:
    """Pull unique req-IDs from free-text (preserves first-seen order)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in text_parts:
        for match in _REQ_ID_RE.findall(part or ""):
            if match not in seen:
                seen.add(match)
                out.append(match)
    return out


def _build_per_req_audit_feedback(
    verdict: "ClosureVerdict",
    conflict_req_ids: list[str],
) -> dict[str, str]:
    """Slice the closure-checker's audit output into per-requirement feedback.

    Each `missing` / `suggested_tasks` entry that mentions a requirement id
    is attributed to that requirement.  Entries mentioning several ids are
    duplicated to every id involved (since the contradiction concerns each).
    The closure rationale is appended to every bucket as shared context so
    the deep-search prompt always sees the overall judgement.

    Returns a dict keyed by requirement id; requirements cited by
    ``conflict_req_ids`` but absent from any entry receive just the rationale.
    """
    rationale = (verdict.rationale or "").strip() or "(no rationale provided)"
    per_req_entries: dict[str, list[str]] = {rid: [] for rid in conflict_req_ids}

    for source_label, items in (
        ("missing", list(verdict.missing)),
        ("suggested", list(verdict.suggested_tasks)),
    ):
        for entry in items:
            if not entry:
                continue
            cited = _extract_req_ids([entry])
            targets = [rid for rid in cited if rid in per_req_entries]
            if not targets:
                # Entry does not name a specific requirement — broadcast to
                # every re-opened req so they all see the shared context.
                targets = list(per_req_entries.keys())
            for rid in targets:
                per_req_entries[rid].append(f"[{source_label}] {entry}")

    out: dict[str, str] = {}
    for rid, entries in per_req_entries.items():
        body = "\n".join(entries) if entries else "(no entry cited this req)"
        out[rid] = (
            f"Closure-checker rationale:\n{rationale}\n\n"
            f"Audit items concerning {rid}:\n{body}\n\n"
            "Instruction: on this rework iteration you MUST reconsider the "
            "prior verdict. Read the cited code regions yourself, and if you "
            "still reach the previous verdict, explicitly cite the code lines "
            "that refute the audit item above.  Do not repeat the same "
            "reasoning path that was driven by the prior verdict."
        )
    return out

from src.agents.closure_checker_agent import _run_closure_checker_async
from src.agents.deep_search_agent import _run_deep_search_async
from src.agents.parser_agent import _run_parser_async
from src.agents.patch_generator_agent import _run_patch_generator_async
from src.agents.patch_planner_agent import _run_patch_planner_async
from src.models.context import EvidenceCards
from src.models.evidence import RequirementItem
from src.models.verdict import ClosureVerdict
from src.orchestrator.audit import build_audit_manifest
from src.orchestrator.guards import (
    DeepSearchBudget,
    check_correct_attribution,
    check_structural_invariants,
    check_sufficiency,
)
from src.orchestrator.states import (
    PipelineState,
    is_valid_transition,
)
from src.tools.ingestion_tools import (
    get_submitted_evidence,
    get_working_memory,
    init_working_memory,
    reset_requirement_for_rework,
    set_evidence_json_path,
    set_repo_root,
    update_localization,
    update_requirement_verdict,
)


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _run_git(repo_dir: Path, *args: str) -> tuple[int, str, str]:
    """Run a git subcommand in *repo_dir*; return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"[orchestrator] git {' '.join(args)} failed: {exc}", flush=True)
        return 1, "", str(exc)

    def _decode(raw: Any) -> str:
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return bytes(raw).decode("utf-8", errors="replace")
        return str(raw or "")

    return result.returncode, _decode(result.stdout), _decode(result.stderr)


def _collect_git_diff(repo_dir: Path, planned_files: list[str] | None = None) -> str:
    """Return `git diff HEAD` of the working tree, including new files.

    Git's plain `git diff` ignores untracked files, so any file that the
    patch-generator *created* (not merely edited) would silently vanish
    from the resulting patch. To capture them, we mark the planned files
    as intent-to-add with ``git add -N`` before diffing — this surfaces
    their full contents as additions — then reset the index afterwards so
    the repo's staging area is unchanged.

    ``planned_files`` is the list of repo-relative paths the patch-planner
    intended to touch. We only promote existing-on-disk files; .gitignored
    paths are force-added (``-f``) because the planner's choice is
    authoritative over ignore rules for patch output.
    """
    added: list[str] = []
    if planned_files:
        existing = [p for p in planned_files if (repo_dir / p).is_file()]
        if existing:
            rc, _, err = _run_git(repo_dir, "add", "-N", "-f", "--", *existing)
            if rc != 0:
                print(
                    f"[orchestrator] git add -N failed (rc={rc}): {err.strip()}",
                    flush=True,
                )
            else:
                added = existing

    rc, diff_text, err = _run_git(repo_dir, "diff", "HEAD")
    if rc != 0:
        print(f"[orchestrator] git diff exit={rc}: {err.strip()}", flush=True)

    if added:
        rc_reset, _, err_reset = _run_git(repo_dir, "reset", "--", *added)
        if rc_reset != 0:
            print(
                f"[orchestrator] git reset (post-diff) failed (rc={rc_reset}): "
                f"{err_reset.strip()}",
                flush=True,
            )

    return diff_text or ""


def _verify_plan_coverage(
    diff_text: str, planned_files: list[str]
) -> list[str]:
    """Return the list of planned files that do NOT appear in *diff_text*.

    A unified diff header looks like ``diff --git a/<path> b/<path>``. We
    match the b-side path because that is the post-edit name (new files
    have ``a/dev/null`` but ``b/<real-path>``). Paths are compared after
    normalizing backslashes to forward slashes.
    """
    if not planned_files:
        return []
    diff_paths: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" b/", 1)
        if len(parts) == 2:
            diff_paths.add(parts[1].strip())
    missing: list[str] = []
    for path in planned_files:
        norm = path.replace("\\", "/")
        if norm not in diff_paths:
            missing.append(norm)
    return missing


def _pick_next_requirement(evidence: EvidenceCards) -> RequirementItem | None:
    """Return the first RequirementItem whose verdict is still UNCHECKED."""
    for req in evidence.requirements:
        if req.verdict == "UNCHECKED":
            return req
    return None


def _build_deep_search_todo(
    target: RequirementItem,
    rework_context: str = "",
) -> str:
    """Build a scoped TODO for one RequirementItem.

    If *rework_context* is non-empty, this requirement was previously given
    a verdict that the closure-checker flagged as inconsistent.  The context
    (closure rationale + conflicting locations + other implicated requirement
    IDs) is appended so the model can deliberately resolve the contradiction
    rather than repeating the prior verdict.
    """
    base = (
        f"Verify RequirementItem against the current codebase.\n\n"
        f"- requirement_id: {target.id}\n"
        f"- origin: {target.origin}\n"
        f"- requirement_text: {target.text}\n\n"
        "Investigate the relevant call chains, data flow and similar "
        "implementations. Decide a verdict among AS_IS_COMPLIANT, "
        "AS_IS_VIOLATED, TO_BE_MISSING, TO_BE_PARTIAL and cite "
        "evidence_locations."
    )
    if rework_context:
        base += (
            "\n\n── REWORK CONTEXT ─────────────────────────────\n"
            "The previous verdict for this requirement was flagged by the "
            "closure-checker as inconsistent with other requirements' "
            "verdicts. Re-read the cited file regions carefully and decide "
            "a verdict that is consistent across the contradicting "
            "requirements. Do not simply repeat the prior verdict.\n\n"
            f"{rework_context}"
        )
    return base


async def _persist_report_findings(report, scope_requirement_id: str) -> None:
    """Persist a DeepSearchReport into evidence cards.

    Writes the per-requirement verdict via update_requirement_verdict, and
    AS-IS code observations (scoped to the same requirement) via
    update_localization.
    """
    if get_submitted_evidence() is None:
        print("[orchestrator] WARNING: no evidence cards to persist into", flush=True)
        return

    # 1) Requirement verdict
    target_id = scope_requirement_id or report.target_requirement_id
    if target_id:
        verdict_args: dict[str, Any] = {
            "requirement_id": target_id,
            "verdict": report.requirement_verdict or "UNCHECKED",
            "evidence_locations": list(report.requirement_evidence_locations),
            "findings": report.requirement_findings or "",
        }
        try:
            await update_requirement_verdict.handler(verdict_args)
        except Exception as exc:
            print(
                f"[orchestrator] update_requirement_verdict.handler failed: {exc}",
                flush=True,
            )

    # 2) AS-IS code observations (scoped)
    loc_args: dict[str, Any] = {"scope_requirement_id": target_id or "unscoped"}
    for attr in (
        "suspect_entities",
        "exact_code_regions",
        "call_chain_context",
        "dataflow_relevant_uses",
        "must_co_edit_relations",
        "dependency_propagation",
        "similar_implementation_patterns",
        "behavioral_constraints",
        "semantic_boundaries",
        "backward_compatibility",
    ):
        values = getattr(report, attr, [])
        if values:
            loc_args[attr] = list(values)

    if len(loc_args) > 1:
        summary = ", ".join(
            f"{k}={len(v)}" for k, v in loc_args.items()
            if isinstance(v, list)
        )
        print(
            f"[orchestrator] persisting deep-search AS-IS findings "
            f"[{target_id}]: {summary}",
            flush=True,
        )
        try:
            await update_localization.handler(loc_args)
        except Exception as exc:
            print(
                f"[orchestrator] update_localization.handler failed: {exc}",
                flush=True,
            )


def _write_patch_outcome(
    output_dir: Path, issue_id: str, patch_outcome: str | None,
    closure_approved: bool,
) -> None:
    """Write patch_outcome.json — safe to call from any exit path."""
    patch_outcome_path = output_dir / "patch_outcome.json"
    patch_outcome_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "issue_id": issue_id,
        "closure_checker_approved": closure_approved,
        "patch_outcome": patch_outcome,
    }
    patch_outcome_path.write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    print(f"[orchestrator] Patch outcome saved -> {patch_outcome_path}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# Code-driven pipeline
# ══════════════════════════════════════════════════════════════════════════

async def run_pipeline(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
) -> Path:
    """Drive the full repair pipeline via a code-driven state-machine loop.

    LLM is only invoked at semantic nodes:
      - deep-search: repository investigation
      - closure-checker: evidence completeness evaluation
      - patch-planner: strategic edit planning
      - patch-generator: applying SEARCH/REPLACE edits

    All flow control (transitions, iteration budget, mechanical checks)
    is enforced by code.
    """
    repo_dir = Path(repo_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Parser produces initial EvidenceCards ──────────────────
    print("[orchestrator] Running parser agent...", flush=True)
    evidence = await _run_parser_async(artifact_text)
    print("[orchestrator] Parser done.", flush=True)

    # ── Step 2: Initialize shared state ────────────────────────────────
    set_repo_root(repo_dir)
    memory = init_working_memory(issue_context=artifact_text, evidence=evidence)
    memory.record_action(phase="parser", outcome="initial_cards_created")

    evidence_path = output_dir / "evidence.json"
    evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    print(f"[orchestrator] Evidence cards saved -> {evidence_path}", flush=True)
    set_evidence_json_path(evidence_path.resolve())

    return await _run_state_machine(
        issue_id=issue_id,
        repo_dir=repo_dir,
        output_dir=output_dir,
        evidence_path=evidence_path,
        memory=memory,
        initial_state=PipelineState.UNDER_SPECIFIED,
    )


async def run_pipeline_from_evidence(
    issue_id: str,
    repo_dir: str | Path,
    output_dir: str | Path,
) -> Path:
    """Resume the pipeline from pre-populated evidence + working memory.

    Skips parser/init: the caller is responsible for having populated
    ``ingestion_tools._working_memory`` and ``_scoped_store`` beforehand.
    Enters the state machine in EVIDENCE_REFINING so the closure-checker
    runs first (which is where a resume is typically useful).
    """
    repo_dir = Path(repo_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    memory = get_working_memory()
    if memory is None:
        raise RuntimeError(
            "run_pipeline_from_evidence called before "
            "ingestion_tools._working_memory was populated."
        )

    evidence_path = (output_dir / "evidence.json").resolve()
    set_evidence_json_path(evidence_path)

    return await _run_state_machine(
        issue_id=issue_id,
        repo_dir=repo_dir,
        output_dir=output_dir,
        evidence_path=evidence_path,
        memory=memory,
        initial_state=PipelineState.EVIDENCE_REFINING,
    )


async def _run_state_machine(
    issue_id: str,
    repo_dir: Path,
    output_dir: Path,
    evidence_path: Path,
    memory,
    initial_state: PipelineState,
) -> Path:
    """Core state-machine loop shared by run_pipeline and run_pipeline_from_evidence."""
    state = initial_state
    budget = DeepSearchBudget(max_iterations=30)
    last_verdict: ClosureVerdict | None = None
    patch_outcome: str | None = None
    forced_closure_done: bool = False
    closure_retry_limit = 2
    closure_failure_streak = 0
    max_closure_failure_streak = 3
    # Rework budget: each EVIDENCE_MISSING with parseable req IDs re-opens
    # those requirements (verdict=UNCHECKED, scope cleared) and feeds the
    # closure-checker's rationale into deep-search as rework context.  The
    # round counter tracks how many rework rounds have been consumed so far.
    rework_rounds_used = 0
    rework_rounds_max = 3

    _terminal_states = (
        PipelineState.PATCH_SUCCESS,
        PipelineState.PATCH_FAILED,
        PipelineState.CLOSURE_FORCED_FAIL,
    )

    while state not in _terminal_states:
        print(f"[orchestrator] State: {state.value}", flush=True)

        # ── UnderSpecified: dispatch deep-search ──────────────────────
        if state == PipelineState.UNDER_SPECIFIED:
            # Budget exhausted: allow exactly one forced closure-checker pass,
            # then terminate regardless of its verdict.
            if budget.is_exhausted():
                print(
                    "[orchestrator] deep-search budget exhausted, "
                    "forcing one closure-checker evaluation",
                    flush=True,
                )
                budget.mark_budget_exhausted()
                assert is_valid_transition(state, PipelineState.EVIDENCE_REFINING)
                state = PipelineState.EVIDENCE_REFINING
                continue

            # Pick the next UNCHECKED requirement to investigate
            current_evidence = get_submitted_evidence()
            target = _pick_next_requirement(current_evidence) if current_evidence else None

            if current_evidence is None:
                print(
                    "[orchestrator] ERROR: evidence cards missing before deep-search; "
                    "terminating safely.",
                    flush=True,
                )
                patch_outcome = "EVIDENCE_INCOMPLETE"
                assert is_valid_transition(state, PipelineState.EVIDENCE_REFINING)
                state = PipelineState.EVIDENCE_REFINING
                continue

            if target is None:
                print(
                    "[orchestrator] All requirements checked — transitioning "
                    "to EVIDENCE_REFINING without new deep-search.",
                    flush=True,
                )
                memory.record_action(
                    phase="deep-search",
                    subagent="deep-search",
                    outcome="skipped_all_requirements_checked",
                )
                assert is_valid_transition(state, PipelineState.EVIDENCE_REFINING)
                state = PipelineState.EVIDENCE_REFINING
                continue

            todo_task = _build_deep_search_todo(
                target,
                rework_context=target.rework_context,
            )
            print(
                f"[orchestrator] Dispatching deep-search for {target.id}: "
                f"{target.text[:80]!r}",
                flush=True,
            )
            try:
                report = await _run_deep_search_async(
                    todo_task, current_evidence, repo_dir,
                )
            except Exception as exc:
                budget.record_iteration()
                print(
                    "[orchestrator] deep-search failed for "
                    f"{target.id}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                memory.record_action(
                    phase="deep-search",
                    subagent="deep-search",
                    outcome=f"iter{budget.iteration}:error:{type(exc).__name__}",
                    requirement_id=target.id,
                )
                assert is_valid_transition(state, PipelineState.EVIDENCE_REFINING)
                state = PipelineState.EVIDENCE_REFINING
                continue

            budget.record_iteration()
            # target.rework_context is cleared by update_requirement_verdict
            # when the new verdict is persisted inside _persist_report_findings.

            # Persist findings — verdict + AS-IS observations under scope
            await _persist_report_findings(report, scope_requirement_id=target.id)
            memory.record_action(
                phase="deep-search",
                subagent="deep-search",
                outcome=f"iter{budget.iteration}:{report.requirement_verdict}",
                requirement_id=target.id,
            )

            # Transition to EvidenceRefining
            assert is_valid_transition(state, PipelineState.EVIDENCE_REFINING)
            state = PipelineState.EVIDENCE_REFINING

        # ── EvidenceRefining: mechanical gates, then closure-checker ──
        elif state == PipelineState.EVIDENCE_REFINING:
            current_evidence = get_submitted_evidence()

            if current_evidence is None:
                print(
                    "[orchestrator] ERROR: evidence cards missing in EvidenceRefining; "
                    "forcing closure fail.",
                    flush=True,
                )
                patch_outcome = "EVIDENCE_INCOMPLETE"
                assert is_valid_transition(state, PipelineState.CLOSURE_FORCED_FAIL)
                state = PipelineState.CLOSURE_FORCED_FAIL
                continue

            # Sufficiency: every requirement has a non-UNCHECKED verdict.
            unchecked = check_sufficiency(current_evidence)
            if unchecked and not budget.is_exhausted():
                print(
                    f"[orchestrator] sufficiency check failed — unchecked: "
                    f"{unchecked[:5]}. Returning to deep-search.",
                    flush=True,
                )
                memory.record_action(
                    phase="evidence-refining",
                    outcome=f"sufficiency_failed:{len(unchecked)}_unchecked",
                )
                assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                state = PipelineState.UNDER_SPECIFIED
                continue

            # Correct attribution: non-compliant verdicts cite at least one location.
            bad_attribution = check_correct_attribution(current_evidence)
            if bad_attribution and not budget.is_exhausted():
                print(
                    f"[orchestrator] correct-attribution check failed: "
                    f"{bad_attribution[:5]}. Returning to deep-search.",
                    flush=True,
                )
                memory.record_action(
                    phase="evidence-refining",
                    outcome=f"attribution_failed:{len(bad_attribution)}_missing_locations",
                )
                assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                state = PipelineState.UNDER_SPECIFIED
                continue

            # ── Structural invariants (phase 18.A) ───────────────────────
            # I2 violations (new_interface + AS_IS_COMPLIANT) are mechanical
            # contradictions → reset to UNCHECKED and re-dispatch deep-search
            # with audit feedback.  I1/I3/I4 are warnings passed into the
            # AuditManifest for the closure-checker to consider.
            structural_failures = check_structural_invariants(current_evidence)
            i2_failures = structural_failures.get("I2", [])
            if i2_failures and not budget.is_exhausted():
                i2_req_ids = _extract_req_ids(i2_failures)
                reset_ids_i2: list[str] = []
                for rid in i2_req_ids:
                    feedback = (
                        "Closure-checker structural invariant I2 violation:\n"
                        "This requirement's origin is 'new_interfaces', which "
                        "means the interface does not exist in the codebase "
                        "yet.  The previous verdict AS_IS_COMPLIANT is a "
                        "contradiction — a nonexistent interface cannot be "
                        "compliant.  Re-investigate and set the verdict to "
                        "TO_BE_MISSING (or TO_BE_PARTIAL if a skeleton exists)."
                    )
                    if reset_requirement_for_rework(rid, audit_feedback=feedback):
                        reset_ids_i2.append(rid)
                if reset_ids_i2:
                    print(
                        f"[orchestrator] structural I2 → reset {reset_ids_i2}; "
                        "returning to deep-search.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=f"I2_reset:{len(reset_ids_i2)}_reqs",
                    )
                    assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                    state = PipelineState.UNDER_SPECIFIED
                    continue

            # I1/I3/I4 warnings flow into the manifest.
            manifest_warnings: list[str] = []
            for key in ("I1", "I3", "I4"):
                manifest_warnings.extend(structural_failures.get(key, []))

            manifest = build_audit_manifest(
                current_evidence,
                structural_warnings=manifest_warnings,
            )
            expected_task_ids = {t.requirement_id for t in manifest.tasks}
            print(
                f"[orchestrator] AuditManifest: {len(manifest.tasks)} tasks "
                f"({sorted(expected_task_ids)}), "
                f"{len(manifest.warnings)} warnings",
                flush=True,
            )

            # Dispatch closure-checker (LLM semantic evaluation)
            print("[orchestrator] Dispatching closure-checker...", flush=True)
            verdict: ClosureVerdict | None = None
            closure_exc: Exception | None = None
            max_attempts = closure_retry_limit + 1
            for attempt in range(1, max_attempts + 1):
                try:
                    verdict = await _run_closure_checker_async(
                        current_evidence, manifest, repo_dir,
                    )
                    closure_exc = None
                    break
                except Exception as exc:
                    closure_exc = exc
                    print(
                        "[orchestrator] closure-checker failed "
                        f"(attempt {attempt}/{max_attempts}): "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    memory.record_action(
                        phase="closure-check",
                        subagent="closure-checker",
                        outcome=(
                            f"error:{type(exc).__name__}:"
                            f"attempt{attempt}/{max_attempts}"
                        ),
                    )
                    if attempt < max_attempts:
                        continue

            if verdict is None:
                closure_failure_streak += 1
                print(
                    "[orchestrator] closure-checker retries exhausted; "
                    f"failure streak={closure_failure_streak}/"
                    f"{max_closure_failure_streak}",
                    flush=True,
                )
                if budget.is_exhausted() or closure_failure_streak >= max_closure_failure_streak:
                    patch_outcome = "EVIDENCE_INCOMPLETE"
                    assert is_valid_transition(
                        state, PipelineState.CLOSURE_FORCED_FAIL
                    )
                    state = PipelineState.CLOSURE_FORCED_FAIL
                else:
                    # Retry closure in-place on next loop without forcing a new deep-search.
                    assert closure_exc is not None
                    memory.record_action(
                        phase="closure-check",
                        subagent="closure-checker",
                        outcome="retry_later_in_evidence_refining",
                    )
                continue

            closure_failure_streak = 0

            last_verdict = verdict
            forced = budget.budget_exhausted and not forced_closure_done
            if forced:
                forced_closure_done = True

            # ── Strict manifest coverage ────────────────────────────────
            # Every task in the manifest must have a corresponding
            # AuditResult; a missing requirement_id means the closure-checker
            # skipped an audit and its verdict cannot be trusted.  Force
            # EVIDENCE_MISSING in that case so the rework path re-dispatches.
            audited_ids = {r.requirement_id for r in verdict.audited}
            uncovered = sorted(expected_task_ids - audited_ids)
            if uncovered:
                print(
                    "[orchestrator] manifest coverage FAIL — audited missing "
                    f"{uncovered}; downgrading verdict to EVIDENCE_MISSING.",
                    flush=True,
                )
                memory.record_action(
                    phase="closure-check",
                    subagent="closure-checker",
                    outcome=f"coverage_fail:{len(uncovered)}_uncovered",
                )
                coverage_msg = (
                    f"[coverage] closure-checker did not return AuditResults "
                    f"for {uncovered}; those requirements remain unaudited."
                )
                # Force-downgrade: synthesize an EVIDENCE_MISSING verdict that
                # reuses the original rationale plus the coverage complaint,
                # and cites the uncovered req IDs so the rework path reopens them.
                verdict = ClosureVerdict(
                    verdict="EVIDENCE_MISSING",
                    rationale=(
                        (verdict.rationale or "").strip()
                        + ("\n\n" if verdict.rationale else "")
                        + coverage_msg
                    ),
                    audited=verdict.audited,
                    missing=list(verdict.missing) + [coverage_msg]
                        + [f"uncovered req {rid}" for rid in uncovered],
                    suggested_tasks=list(
                        dict.fromkeys(list(verdict.suggested_tasks) + uncovered)
                    ),
                )
                last_verdict = verdict

            if verdict.verdict == "CLOSURE_APPROVED":
                print("[orchestrator] CLOSURE_APPROVED", flush=True)
                memory.record_action(
                    phase="closure-check",
                    subagent="closure-checker",
                    outcome="CLOSURE_APPROVED" + ("_forced" if forced else ""),
                )
                assert is_valid_transition(state, PipelineState.CLOSED)
                state = PipelineState.CLOSED
            else:
                print(
                    f"[orchestrator] EVIDENCE_MISSING: "
                    f"{', '.join(verdict.missing[:3])}",
                    flush=True,
                )
                memory.record_action(
                    phase="closure-check",
                    subagent="closure-checker",
                    outcome="EVIDENCE_MISSING" + ("_forced" if forced else ""),
                )
                if forced:
                    # Budget already exhausted and closure still failed —
                    # do NOT loop back to deep-search. Terminate cleanly.
                    patch_outcome = "EVIDENCE_INCOMPLETE"
                    assert is_valid_transition(
                        state, PipelineState.CLOSURE_FORCED_FAIL
                    )
                    state = PipelineState.CLOSURE_FORCED_FAIL
                else:
                    # Rework path: extract req IDs cited in the closure's
                    # `missing` / `suggested_tasks`, reset those requirements
                    # to UNCHECKED, stash the rationale so the next
                    # deep-search can see the contradiction context.
                    conflict_req_ids = _extract_req_ids(
                        verdict.missing + verdict.suggested_tasks
                    )
                    if rework_rounds_used < rework_rounds_max and conflict_req_ids:
                        per_req_feedback = _build_per_req_audit_feedback(
                            verdict, conflict_req_ids,
                        )
                        reset_ids: list[str] = []
                        for rid in conflict_req_ids:
                            if reset_requirement_for_rework(
                                rid,
                                audit_feedback=per_req_feedback.get(rid, ""),
                            ):
                                reset_ids.append(rid)
                    else:
                        reset_ids = []

                    if reset_ids:
                        rework_rounds_used += 1
                        print(
                            "[orchestrator] EVIDENCE_MISSING → rework: "
                            f"re-opened {reset_ids} "
                            f"(round {rework_rounds_used}/{rework_rounds_max})",
                            flush=True,
                        )
                        memory.record_action(
                            phase="closure-check",
                            subagent="closure-checker",
                            outcome=(
                                f"rework:reopen={len(reset_ids)}_reqs:"
                                f"round={rework_rounds_used}/{rework_rounds_max}"
                            ),
                        )
                        assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                        state = PipelineState.UNDER_SPECIFIED
                    else:
                        reason = (
                            "no parseable req IDs in closure output"
                            if not conflict_req_ids
                            else "rework rounds exhausted"
                        )
                        print(
                            f"[orchestrator] EVIDENCE_MISSING unresolved "
                            f"({reason}); terminating as CLOSURE_FORCED_FAIL.",
                            flush=True,
                        )
                        memory.record_action(
                            phase="closure-check",
                            subagent="closure-checker",
                            outcome=f"EVIDENCE_MISSING_terminal:{reason}",
                        )
                        patch_outcome = "EVIDENCE_INCOMPLETE"
                        assert is_valid_transition(
                            state, PipelineState.CLOSURE_FORCED_FAIL
                        )
                        state = PipelineState.CLOSURE_FORCED_FAIL

        # ── Closed: dispatch patch-planner ────────────────────────────
        elif state == PipelineState.CLOSED:
            print("[orchestrator] Dispatching patch-planner...", flush=True)
            plan = await _run_patch_planner_async(memory)
            memory.record_action(
                phase="patch-planning",
                subagent="patch-planner",
                outcome=f"{len(plan.edits)}_files_planned",
            )

            assert is_valid_transition(state, PipelineState.PATCH_PLANNING)
            state = PipelineState.PATCH_PLANNING

        # ── PatchPlanning: dispatch patch-generator ───────────────────
        elif state == PipelineState.PATCH_PLANNING:
            print("[orchestrator] Dispatching patch-generator...", flush=True)
            success = await _run_patch_generator_async(memory, repo_dir)
            if success:
                memory.record_action(
                    phase="patch-generation",
                    subagent="patch-generator",
                    outcome="PATCH_SUCCESS",
                )
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
            else:
                memory.record_action(
                    phase="patch-generation",
                    subagent="patch-generator",
                    outcome="PATCH_FAILED",
                )
                patch_outcome = "PATCH_FAILED"
                assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                state = PipelineState.PATCH_FAILED

    # ── Step 4: Post-pipeline finalization ─────────────────────────────
    print(f"[orchestrator] Pipeline finished: {state.value}", flush=True)

    closure_approved = (state in (PipelineState.PATCH_SUCCESS, PipelineState.PATCH_FAILED)
                        and last_verdict is not None
                        and last_verdict.verdict == "CLOSURE_APPROVED")

    if state == PipelineState.CLOSURE_FORCED_FAIL and patch_outcome is None:
        patch_outcome = "EVIDENCE_INCOMPLETE"

    # Save final evidence
    current_evidence = get_submitted_evidence()
    if current_evidence is not None:
        evidence_path.resolve().write_text(
            current_evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"[orchestrator] Final evidence JSON saved -> {evidence_path}", flush=True)

    # Save working memory
    wm = get_working_memory()
    if wm is not None:
        wm_path = output_dir / "working_memory.json"
        wm_path.write_text(wm.model_dump_json(indent=2), encoding="utf-8")
        print(
            f"[orchestrator] Working memory saved -> {wm_path} "
            f"({len(wm.retrieved_code)} cached snippets, "
            f"{len(wm.action_history)} actions)",
            flush=True,
        )
        if wm.patch_plan is not None:
            plan_path = output_dir / "patch_plan.json"
            plan_path.write_text(wm.patch_plan.model_dump_json(indent=2), encoding="utf-8")
            print(f"[orchestrator] Patch plan saved -> {plan_path}", flush=True)

    # Collect git diff \u2014 pass planned files so newly-created files are
    # promoted from untracked to intent-to-add and surface in the diff.
    planned_files: list[str] = []
    if wm.patch_plan is not None:
        planned_files = [edit.filepath for edit in wm.patch_plan.edits]
    diff_text = _collect_git_diff(repo_dir, planned_files=planned_files)
    if diff_text.startswith("\ufeff"):
        diff_text = diff_text.lstrip("\ufeff")

    # Plan-coverage check: every planned file must appear in the diff.
    # If the generator silently dropped an edit (e.g. SEARCH mismatch it
    # failed to recover from), the outcome is a partial patch; downgrade
    # PATCH_SUCCESS so downstream eval sees the real state.
    missing_from_diff = _verify_plan_coverage(diff_text, planned_files)
    if missing_from_diff and patch_outcome == "PATCH_SUCCESS":
        print(
            "[orchestrator] WARNING: planned files missing from patch.diff: "
            f"{missing_from_diff}. Downgrading PATCH_SUCCESS -> PATCH_FAILED.",
            flush=True,
        )
        patch_outcome = "PATCH_FAILED"
        closure_approved = False

    patch_path = output_dir / "patch.diff"
    patch_path.write_text(diff_text, encoding="utf-8")
    if diff_text:
        print(f"[orchestrator] patch.diff saved -> {patch_path} ({len(diff_text)} bytes)", flush=True)
    else:
        print(f"[orchestrator] WARNING: empty patch.diff -> {patch_path}", flush=True)

    # Now that any plan-coverage downgrade has been applied, persist the
    # final patch outcome.
    _write_patch_outcome(output_dir, issue_id, patch_outcome, closure_approved)

    # Write prediction.json for SWE-bench eval format
    prediction_path = output_dir / "prediction.json"
    prediction_path.write_text(
        json.dumps({"instance_id": issue_id, "model_patch": diff_text}, indent=2),
        encoding="utf-8",
    )
    print(f"[orchestrator] prediction.json saved -> {prediction_path}", flush=True)

    return evidence_path


def run_orchestrator(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
) -> Path:
    """Synchronous entry-point. Returns the path to evidence.json."""
    return asyncio.run(
        run_pipeline(
            issue_id=issue_id,
            repo_dir=repo_dir,
            artifact_text=artifact_text,
            output_dir=output_dir,
        )
    )
