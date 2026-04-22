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
    audit_results: list | None = None,
) -> dict[str, str]:
    """Slice the closure-checker's audit output into per-requirement feedback.

    Phase 18.F differentiated feedback — routes different audit failure types
    to different rework instruction templates.

    Types:
    - new_interface_cannot_be_compliant → parser marked new interface, must be TO_BE_MISSING
    - findings_anti_hallucination → findings引用的片段在代码中不存在，必须删除或改写
    - prescriptive_boundary_self_check → prescriptive fix 在某边界下不满足 requirement

    Returns a dict keyed by requirement id.
    """
    rationale = (verdict.rationale or "").strip() or "(no rationale provided)"
    per_req_entries: dict[str, list[str]] = {rid: [] for rid in conflict_req_ids}

    # First, collect entries from verdict.missing / suggested_tasks
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
                targets = list(per_req_entries.keys())
            for rid in targets:
                per_req_entries[rid].append(f"[{source_label}] {entry}")

    # Phase 18.F: Extract per-check failure info from AuditResult
    audit_by_req: dict[str, dict] = {}
    if audit_results:
        for res in audit_results:
            audit_by_req[res.requirement_id] = {
                "per_check": res.per_check,
                "failures": res.failures,
            }

    out: dict[str, str] = {}
    for rid, entries in per_req_entries.items():
        body = "\n".join(entries) if entries else "(no entry cited this req)"

        # ── Phase 18.F: Differentiated rework instructions ────────────────
        differentiated_instruction = ""
        audit_info = audit_by_req.get(rid, {})

        # Check for new_interface_cannot_be_compliant
        for entry in entries:
            if "I2_new_interface_cannot_be_compliant" in entry or "new_interface" in entry.lower():
                differentiated_instruction = (
                    "REWORK INSTRUCTION (new_interface): "
                    "Parser marked this interface as NEW (to be implemented). "
                    "AS_IS_COMPLIANT is impossible — new interfaces do not exist yet. "
                    "Re-verify: if you found a SIMILAR function (e.g. getObjects vs mget), "
                    "change verdict to TO_BE_MISSING. "
                    "If exact match exists, cite the exact definition line and explain "
                    "why parser misclassified it."
                )
                break

        # Check for findings_anti_hallucination
        per_check = audit_info.get("per_check", {})
        failures_list = audit_info.get("failures", [])
        if per_check.get("findings_anti_hallucination") == "FAIL":
            if not differentiated_instruction:
                for fail_desc in failures_list:
                    if "anti_hallucination" in fail_desc.lower() or "not found" in fail_desc.lower():
                        snippet_match = re.search(r"`([^`]+)`", fail_desc)
                        snippet = snippet_match.group(1) if snippet_match else "<unknown snippet>"
                        differentiated_instruction = (
                            f"REWORK INSTRUCTION (anti_hallucination): "
                            f"Your findings claimed code snippet `{snippet}` exists, "
                            f"but audit could not verify it in cited file regions. "
                            f"This round, your findings MUST ONLY reference code "
                            f"that you Read and confirmed. "
                            f"Delete or rephrase unverified snippets. "
                            f"Do NOT infer or recall code from memory."
                        )
                        break
                if not differentiated_instruction:
                    differentiated_instruction = (
                        "REWORK INSTRUCTION (anti_hallucination): "
                        "Your findings contain unverified code snippets. "
                        "This round, cite only code you actually Read. "
                        "Do NOT fabricate code that isn't there."
                    )

        # Check for prescriptive_boundary_self_check
        if per_check.get("prescriptive_boundary_self_check") == "FAIL":
            if not differentiated_instruction:
                for fail_desc in failures_list:
                    if "boundary" in fail_desc.lower() or "edge" in fail_desc.lower():
                        differentiated_instruction = (
                            "REWORK INSTRUCTION (prescriptive_boundary): "
                            "Your prescriptive fix fails a boundary condition. "
                            "Before deciding verdict, enumerate ≥2 boundary cases "
                            "(null/undefined, empty set, max value, etc.). "
                            "For each boundary, explicitly write 'pass/fail + why'. "
                            "If any boundary fails, reconsider your prescriptive "
                            "and explain how to fix it."
                        )
                        break

        if not differentiated_instruction:
            differentiated_instruction = (
                "Instruction: re-verify the cited code regions. "
                "If you still reach the prior verdict, cite exact code lines "
                "that refute the audit finding."
            )

        out[rid] = (
            f"Closure-checker rationale:\n{rationale}\n\n"
            f"Audit items concerning {rid}:\n{body}\n\n"
            f"{differentiated_instruction}"
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
from src.orchestrator.guards import (
    DeepSearchBudget,
    check_correct_attribution,
    check_sufficiency,
    check_structural_invariants,
)
from src.orchestrator.states import (
    PipelineState,
    is_valid_transition,
)
from src.orchestrator.audit import build_audit_manifest
from src.models.audit import AuditManifest
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

def _collect_git_diff(repo_dir: Path) -> str:
    """Return `git diff` of the working tree, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "diff"],
            capture_output=True,
            check=False,
        )
        stdout = result.stdout
        if isinstance(stdout, (bytes, bytearray, memoryview)):
            diff_text = bytes(stdout).decode("utf-8", errors="replace")
        else:
            diff_text = str(stdout)
    except (FileNotFoundError, OSError) as exc:
        print(f"[orchestrator] git diff failed: {exc}", flush=True)
        return ""
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, (bytes, bytearray, memoryview)):
            stderr_text = bytes(stderr).decode("utf-8", errors="replace")
        else:
            stderr_text = str(stderr)
        print(f"[orchestrator] git diff exit={result.returncode}: {stderr_text}", flush=True)
    return diff_text or ""


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

    # ── Step 3: Code-driven state-machine loop ─────────────────────────
    state = PipelineState.UNDER_SPECIFIED
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

            # ── Phase 18.A: Structural invariants ────────────────────────
            structural_failures = check_structural_invariants(current_evidence)
            i2_hits = structural_failures.get("I2", [])
            i1_i3_warnings = []
            for k in ("I1", "I3"):
                i1_i3_warnings.extend(structural_failures.get(k, []))

            # I2 violations: new_interface reqs marked AS_IS_COMPLIANT must be reset.
            if i2_hits and not budget.is_exhausted():
                reset_ids_i2: list[str] = []
                for line in i2_hits:
                    # Parse "I2_new_interface_cannot_be_compliant: req-XXX (names=...)"
                    match = _REQ_ID_RE.search(line)
                    if match:
                        rid = match.group(0)
                        audit_feedback = (
                            "Parser marked this interface as new (to be implemented). "
                            "AS_IS_COMPLIANT is impossible — new interfaces do not exist yet. "
                            "Re-verify: if you found a similar but not exact function, "
                            "change verdict to TO_BE_MISSING. If exact match, check parser."
                        )
                        if reset_requirement_for_rework(rid, audit_feedback=audit_feedback):
                            reset_ids_i2.append(rid)
                if reset_ids_i2:
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=f"I2_reset:{reset_ids_i2}",
                    )
                    print(
                        f"[orchestrator] I2 invariant: reset {reset_ids_i2} "
                        "to UNCHECKED for rework.",
                        flush=True,
                    )
                    assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                    state = PipelineState.UNDER_SPECIFIED
                    continue

            # ── Phase 18.B: Build AuditManifest ───────────────────────────
            manifest: AuditManifest = build_audit_manifest(
                current_evidence,
                structural_warnings=i1_i3_warnings,
            )
            manifest_ids = {t.requirement_id for t in manifest.tasks}

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

            # ── Phase 18.B: Validate manifest coverage ─────────────────────
            audited_ids = {r.requirement_id for r in verdict.audited}
            if manifest_ids != audited_ids:
                missing_audit = list(manifest_ids - audited_ids)
                extra_audit = list(audited_ids - manifest_ids)
                print(
                    f"[orchestrator] Closure-checker audit coverage mismatch: "
                    f"missing={missing_audit}, extra={extra_audit}",
                    flush=True,
                )
                memory.record_action(
                    phase="closure-check",
                    subagent="closure-checker",
                    outcome=f"audit_coverage_mismatch:missing={missing_audit}",
                )
                # Treat as EVIDENCE_MISSING — the unaudited tasks are defects.
                if not forced and rework_rounds_used < rework_rounds_max:
                    for rid in missing_audit:
                        reset_requirement_for_rework(
                            rid,
                            audit_feedback="Closure-checker did not audit this requirement. "
                            "Please re-verify and ensure it is in the audit output.",
                        )
                    rework_rounds_used += 1
                    assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                    state = PipelineState.UNDER_SPECIFIED
                    continue
                else:
                    patch_outcome = "EVIDENCE_INCOMPLETE"
                    assert is_valid_transition(state, PipelineState.CLOSURE_FORCED_FAIL)
                    state = PipelineState.CLOSURE_FORCED_FAIL
                    continue

            # ── Check for any FAIL in AuditResult.per_check ────────────────
            any_fail = False
            failed_req_ids: list[str] = []
            for audit_res in verdict.audited:
                for check_type, outcome in audit_res.per_check.items():
                    if outcome == "FAIL":
                        any_fail = True
                        failed_req_ids.append(audit_res.requirement_id)
                        # Append detailed failure to verdict.missing if not already there
                        for fail_desc in audit_res.failures:
                            if fail_desc not in verdict.missing:
                                verdict.missing.append(
                                    f"{audit_res.requirement_id}: [{check_type}] {fail_desc}"
                                )

            if any_fail and verdict.verdict == "CLOSURE_APPROVED":
                # Force verdict to EVIDENCE_MISSING if any check failed
                verdict.verdict = "EVIDENCE_MISSING"
                verdict.rationale = (
                    "At least one audit check failed. "
                    f"Failed requirements: {failed_req_ids}"
                )

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
                            audit_results=list(verdict.audited),
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

    # Record patch outcome
    _write_patch_outcome(output_dir, issue_id, patch_outcome, closure_approved)

    # Collect git diff
    diff_text = _collect_git_diff(repo_dir)
    if diff_text.startswith("\ufeff"):
        diff_text = diff_text.lstrip("\ufeff")
    patch_path = output_dir / "patch.diff"
    patch_path.write_text(diff_text, encoding="utf-8")
    if diff_text:
        print(f"[orchestrator] patch.diff saved -> {patch_path} ({len(diff_text)} bytes)", flush=True)
    else:
        print(f"[orchestrator] WARNING: empty patch.diff -> {patch_path}", flush=True)

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
