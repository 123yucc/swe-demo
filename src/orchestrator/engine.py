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
from dataclasses import dataclass, field
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


# ── Phase 25: dimension → operator → reset-scope mapping ───────────────────

# The closed enum of conflicting fields the LLM may name, mapped to the reset
# scope the deterministic reset tool actually supports ({"findings"} or full).
# Anything other than a pure findings-level conflict is a full reset.
_FINDINGS_ONLY_FIELDS = frozenset({"findings"})


@dataclass
class RworkSpec:
    """One requirement's rework instruction derived from a closure verdict.

    operator distinguishes the two failure classes the phase-25 design encodes:
      * ``deepen``    — Sufficiency FAIL: the investigation is too thin; redo it
                        fully (explore/converge to close the evidence gap).
      * ``reconcile`` — Consistency / prescriptive FAIL: the evidence conflicts;
                        re-ground the implicated requirement(s) with a reasoning
                        path different from the rejected verdict.
    Both physically reuse ``reset_requirement_for_rework`` — only fields_to_reset
    and the feedback template differ.
    """

    operator: str  # "deepen" | "reconcile"
    fields_to_reset: set[str] | None  # None = full reset, {"findings"} = conclusion only
    feedback: str

    def merge(self, other: "RworkSpec") -> "RworkSpec":
        """Combine two specs for the same requirement; prefer the wider reset."""
        # Full reset (None) dominates findings-only; deepen dominates reconcile.
        wider = None if (self.fields_to_reset is None or other.fields_to_reset is None) else self.fields_to_reset
        op = "deepen" if "deepen" in (self.operator, other.operator) else "reconcile"
        fb = self.feedback if self.feedback == other.feedback else f"{self.feedback}\n{other.feedback}"
        return RworkSpec(operator=op, fields_to_reset=wider, feedback=fb)


def _conflicting_field_to_scope(conflicting_field: str | None) -> set[str] | None:
    """Map a DimensionFinding.conflicting_field enum value to a reset scope."""
    if conflicting_field in _FINDINGS_ONLY_FIELDS:
        return {"findings"}
    return None


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


def _derive_rework_specs(
    verdict: "ClosureVerdict",
) -> dict[str, RworkSpec]:
    """Map a closure verdict's dimension findings to per-requirement rework specs.

    Phase 25 mapping (deterministic — the LLM reports only dimension + enum
    field + relevant reqs; code decides reset scope and operator):

      | dimension    | operator   | reset scope                              |
      |--------------|------------|------------------------------------------|
      | sufficiency  | deepen     | full reset (None) of each named req      |
      | consistency  | reconcile  | <cross-req> or multi-req → full reset    |
      |              |            | all named; else map conflicting_field    |
      | prescriptive | reconcile  | {"findings"} (conclusion only)           |
      | (audited)    |            |                                          |

    Returns ``{requirement_id: RworkSpec}``.
    """
    specs: dict[str, RworkSpec] = {}

    def _add(rid: str, spec: RworkSpec) -> None:
        if not rid or rid == "<cross-req>":
            return
        specs[rid] = specs[rid].merge(spec) if rid in specs else spec

    for finding in verdict.dimension_findings:
        if finding.status != "FAIL":
            continue
        explanation = (finding.explanation or "").strip() or "(no explanation)"
        field_label = finding.conflicting_field or "evidence"
        if finding.dimension == "sufficiency":
            feedback = (
                "Sufficiency FAIL — the evidence does not yet support a single "
                f"correct repair commit: {explanation}. Deepen the investigation "
                f"(localization / constraints) for {field_label}."
            )
            targets = finding.requirement_ids or []
            for rid in targets:
                _add(rid, RworkSpec("deepen", None, feedback))
        elif finding.dimension == "consistency":
            cross = (
                finding.conflicting_field == "<cross-req>"
                or len(finding.requirement_ids) > 1
            )
            scope = None if cross else _conflicting_field_to_scope(finding.conflicting_field)
            feedback = (
                f"Consistency FAIL on {field_label}: {explanation}. This round "
                "you MUST produce a reasoning path different from the prior "
                "verdict so the contradiction resolves."
            )
            for rid in finding.requirement_ids:
                _add(rid, RworkSpec("reconcile", scope, feedback))

    # Prescriptive boundary check (per-task AuditResult) → findings-only reconcile.
    for result in verdict.audited:
        checks = result.per_check or {}
        if checks.get("prescriptive_boundary_self_check") == "FAIL":
            fb = (
                "Prescriptive boundary FAIL — the proposed fix fails an edge "
                "case. Re-verify the prescriptive findings against boundary "
                "conditions and rewrite the conclusion."
            )
            _add(result.requirement_id, RworkSpec("reconcile", {"findings"}, fb))

    return specs


from src.agents.closure_checker_agent import _run_closure_checker_async
from src.agents.custom_router_agent import run_custom_router
from src.agents.deep_search_agent import _run_deep_search_async
from src.agents.ltm_agent import run_agentic_ltm_retrieval
from src.agents.parser_agent import _run_parser_async
from src.agents.patch_generator_agent import _run_patch_generator_async
from src.agents.patch_planner_agent import _run_patch_planner_async
from src.memory import (
    Experience,
    append_custom_recommendations_log,
    append_recommendations_log,
    format_custom_rules_for_prompt,
    format_experiences_for_prompt,
    load_custom_rules,
    select_matching_rules,
)
from src.models.context import EvidenceCards
from src.models.custom_rules import RouteTags
from src.models.evidence import RequirementItem
from src.models.verdict import ClosureVerdict
from src.orchestrator.audit import build_audit_manifest
from src.orchestrator.build_verify import (
    BuildCheckResult,
    detect_build_system,
    diff_new_errors,
    render_errors_for_feedback,
    run_build_check,
)
from src.orchestrator.consistency_checks import (
    check_consistency_anchors,
    check_rename_residue,
    render_residue_for_feedback,
)
from src.orchestrator.grounding import run_static_grounding
from src.orchestrator.dynamic_grounding import run_dynamic_grounding
from src.orchestrator.guards import (
    DeepSearchBudget,
    check_consistency_anchors_format,
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


def _strip_mode_only_hunks(diff_text: str) -> str:
    """Remove diff entries that contain only file-mode changes or submodule dirty markers.

    On Windows/NTFS, git may report spurious 755->644 mode changes that
    pollute patch.diff. Submodule -dirty markers are also noise when the
    patch did not intentionally update the submodule pointer.
    """
    if not diff_text:
        return diff_text
    entries: list[str] = []
    current_lines: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current_lines:
            entries.append("".join(current_lines))
            current_lines = []
        current_lines.append(line)
    if current_lines:
        entries.append("".join(current_lines))

    kept: list[str] = []
    for entry in entries:
        lines = entry.splitlines()
        has_content = any(
            (l.startswith("+") or l.startswith("-"))
            and not l.startswith("--- ")
            and not l.startswith("+++ ")
            and "Subproject commit " not in l
            for l in lines
        )
        if has_content:
            kept.append(entry)
    return "".join(kept)


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

    diff_text = _strip_mode_only_hunks(diff_text or "")

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


def _compute_baseline_build(
    repo_dir: Path,
    system: str,
) -> BuildCheckResult | None:
    """Compute the build result at clean base_commit, restoring the patch after.

    The post-patch working tree is ``base + patch`` (tracked edits plus any
    files the generator created). To get a baseline we stash everything
    (``-u`` includes untracked created files), run the same build on the clean
    base tree, then pop the stash to restore the patch.

    Returns the baseline BuildCheckResult, or None if the stash failed (in
    which case the caller conservatively treats every post error as new — i.e.
    assumes base_commit compiles, which is true for any real released commit).
    """
    rc, out, err = _run_git(repo_dir, "stash", "push", "-u", "-m", "build-verify-baseline")
    combined = f"{out}\n{err}".lower()
    if rc != 0:
        print(
            f"[build-verify] git stash failed (rc={rc}); cannot compute "
            f"baseline, treating all errors as new: {err.strip()}",
            flush=True,
        )
        return None
    if "no local changes" in combined:
        # Nothing to stash — working tree already clean base. No patch to
        # diff against; treat all post errors as new.
        print(
            "[build-verify] nothing to stash for baseline (clean tree); "
            "treating all errors as new",
            flush=True,
        )
        return None

    try:
        baseline = run_build_check(repo_dir, system)
        print(
            f"[build-verify] baseline build at base_commit: ok={baseline.ok} "
            f"errors={len(baseline.errors)}",
            flush=True,
        )
        return baseline
    finally:
        pop_rc, _, pop_err = _run_git(repo_dir, "stash", "pop")
        if pop_rc != 0:
            print(
                f"[build-verify] CRITICAL: git stash pop failed (rc={pop_rc}); "
                f"working tree may be missing the patch: {pop_err.strip()}",
                flush=True,
            )


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
            "verdict": report.requirement_verdict or "TO_BE_PARTIAL",
            "evidence_locations": list(report.requirement_evidence_locations),
            "findings": report.requirement_findings or "",
        }
        try:
            result = await update_requirement_verdict.handler(verdict_args)
            # Handler returns error dict (not exception) when validation fails.
            # Most common: non-compliant verdict with empty evidence_locations.
            result_text = ""
            if isinstance(result, dict):
                for item in result.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "text":
                        result_text = item.get("text", "")
            if "ERROR" in result_text:
                print(
                    f"[orchestrator] update_requirement_verdict rejected: "
                    f"{result_text[:120]}",
                    flush=True,
                )
                # Fallback: if locations are empty but verdict is not compliant,
                # use exact_code_regions as fallback locations.
                if "evidence_locations must be non-empty" in result_text:
                    fallback_locs = list(report.exact_code_regions or [])
                    if fallback_locs:
                        verdict_args["evidence_locations"] = fallback_locs
                        await update_requirement_verdict.handler(verdict_args)
                        print(
                            f"[orchestrator] retried with exact_code_regions as "
                            f"evidence_locations ({len(fallback_locs)} locs).",
                            flush=True,
                        )
                    else:
                        # Last resort: write directly to the requirement object,
                        # bypassing the handler's non-empty check. This prevents
                        # infinite loops for TO_BE_MISSING requirements where the
                        # agent cannot point to specific lines.
                        evidence = get_submitted_evidence()
                        if evidence:
                            for req in evidence.requirements:
                                if req.id == target_id:
                                    req.verdict = verdict_args["verdict"]
                                    req.findings = verdict_args["findings"]
                                    print(
                                        f"[orchestrator] wrote verdict "
                                        f"{verdict_args['verdict']} directly "
                                        f"(no evidence_locations available).",
                                        flush=True,
                                    )
                                    break
        except Exception as exc:
            print(
                f"[orchestrator] update_requirement_verdict.handler failed: {exc}",
                flush=True,
            )

    # 2) AS-IS code observations (scoped).  Compliant requirements are stored
    # as lightweight coverage status, not as patch-planning material.
    if report.requirement_verdict == "AS_IS_COMPLIANT":
        return

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


async def _progressive_retrieve_ltm(
    *,
    stage: str,
    query: str,
    output_dir: Path,
    summary_top_k: int = 8,
    detail_top_k: int = 3,
) -> tuple[list[str], list[Experience]]:
    """MemGovern-style progressive retrieval via a multi-turn LTM agent."""
    try:
        summaries, selected_ids, experiences = await run_agentic_ltm_retrieval(
            stage=stage,
            query_text=query,
            output_dir=output_dir,
            max_turns=max(10, summary_top_k),
            max_budget_usd=1.0,
        )
        log_path = append_recommendations_log(
            output_dir,
            stage=stage,
            query=query,
            search_summaries=summaries,
            selected_ids=selected_ids,
            experiences=experiences,
        )
        print(
            f"[ltm] recorded retrieval stage={stage} -> {log_path} "
            f"(selected_ids={selected_ids})",
            flush=True,
        )
        return summaries, experiences
    except Exception as exc:
        log_path = append_recommendations_log(
            output_dir,
            stage=stage,
            query=query,
            search_summaries=[],
            selected_ids=[],
            experiences=[],
            error=f"{type(exc).__name__}: {exc}",
        )
        print(
            f"[ltm] agentic retrieval failed at stage={stage}: {exc}",
            flush=True,
        )
        print(
            f"[ltm] wrote failure record -> {log_path}",
            flush=True,
        )
        return [], []


async def _route_and_match_custom_rules(
    *,
    stage: str,
    query: str,
    output_dir: Path,
) -> str:
    """Run the custom-rule router, match against the local rule library,
    and return a rendered prompt block (or "" if nothing matched).

    Logs every invocation to ``custom_recommendations.json``. Any failure
    (router error, file missing, etc.) is recorded in the same log with
    an ``error`` field; the function then returns "" so the pipeline
    continues without injection.
    """
    rules = load_custom_rules()
    if not rules:
        append_custom_recommendations_log(
            output_dir,
            stage=stage,
            query=query,
            route=None,
            matched_ids=[],
            error="no_custom_rules_loaded",
        )
        return ""

    try:
        route = await run_custom_router(query)
    except Exception as exc:
        append_custom_recommendations_log(
            output_dir,
            stage=stage,
            query=query,
            route=None,
            matched_ids=[],
            error=f"{type(exc).__name__}: {exc}",
        )
        print(
            f"[custom-route] router failed at stage={stage}: {exc}",
            flush=True,
        )
        return ""

    matched = select_matching_rules(rules, route)
    block = format_custom_rules_for_prompt(matched)

    log_path = append_custom_recommendations_log(
        output_dir,
        stage=stage,
        query=query,
        route=route.model_dump(),
        matched_ids=[r.id for r in matched],
    )
    print(
        f"[custom-route] stage={stage} matched={len(matched)}/{len(rules)} "
        f"-> {log_path}",
        flush=True,
    )
    return block


# ══════════════════════════════════════════════════════════════════════════
# Code-driven pipeline
# ══════════════════════════════════════════════════════════════════════════

async def run_pipeline(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
    problem_statement: str = "",
) -> Path:
    """Drive the full repair pipeline via a code-driven state-machine loop.

    LLM is only invoked at semantic nodes:
      - deep-search: repository investigation
      - closure-checker: evidence completeness evaluation
      - patch-planner: strategic edit planning
      - patch-generator: applying SEARCH/REPLACE edits

    All flow control (transitions, iteration budget, mechanical checks)
    is enforced by code.

    ``problem_statement`` is the raw issue body used as the long-term-memory
    semantic search query.  Falls back to ``artifact_text`` when empty.
    """
    repo_dir = Path(repo_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ltm_query = (problem_statement or artifact_text).strip()
    # custom-rule routing benefits from seeing requirements/interface text
    # (e.g. specific function names like db.mget, field names like
    # email:pending) which tell the LLM what change shapes will be
    # involved. The bug-shape ChromaDB path is happier with the bare
    # problem_statement, so we keep them separate.
    custom_route_query = artifact_text.strip()

    # ── Step 0: long-term memory retrieval (early-stage) ───────────────
    # Pull top-5 similar past bugs and inject the bug_description block as
    # reference reading material for the deep-search rounds. fix_experience
    # is NOT pulled here — it would only confuse the investigation phase
    # before evidence has been collected.
    early_summaries, early_experiences = await _progressive_retrieve_ltm(
        stage="under_specified",
        query=ltm_query,
        output_dir=output_dir,
        summary_top_k=5,
        detail_top_k=3,
    )
    early_ltm_block = format_experiences_for_prompt(
        early_experiences, include_fix=False,
    )

    # ── Step 0b: parallel custom-rule tag-routing path ─────────────────
    # Independent of ChromaDB; classifies the case on three axes and
    # injects matched hand-written repair-discipline rules.
    early_custom_block = await _route_and_match_custom_rules(
        stage="under_specified",
        query=custom_route_query,
        output_dir=output_dir,
    )

    # ── Step 1: Parser produces initial EvidenceCards ──────────────────
    print("[orchestrator] Running parser agent...", flush=True)
    evidence = await _run_parser_async(artifact_text)
    print("[orchestrator] Parser done.", flush=True)

    # ── Step 2: Initialize shared state ────────────────────────────────
    set_repo_root(repo_dir)
    memory = init_working_memory(issue_context=artifact_text, evidence=evidence)
    memory.ltm_search_summaries = early_summaries
    if early_ltm_block:
        # Stash on memory so deep-search can prepend it to its TODO without
        # re-issuing the network call. Will be replaced before patch-planning
        # with a fix-experience-bearing block (top-3 with full text).
        memory.ltm_reference_block = early_ltm_block
    if early_custom_block:
        memory.custom_repair_block = early_custom_block
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
        ltm_query=ltm_query,
        custom_route_query=custom_route_query,
    )


async def run_pipeline_from_evidence(
    issue_id: str,
    repo_dir: str | Path,
    output_dir: str | Path,
    problem_statement: str = "",
) -> Path:
    """Resume the pipeline from pre-populated evidence + working memory.

    Skips parser/init: the caller is responsible for having populated
    ``ingestion_tools._working_memory`` beforehand (scoped evidence lives on
    each RequirementItem.scoped_evidence).
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
        ltm_query=(problem_statement or memory.issue_context).strip(),
    )


async def _run_state_machine(
    issue_id: str,
    repo_dir: Path,
    output_dir: Path,
    evidence_path: Path,
    memory,
    initial_state: PipelineState,
    ltm_query: str = "",
    custom_route_query: str = "",
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

    # Per-requirement retry counter: prevents infinite loops where the same
    # requirement keeps returning UNCHECKED and starving other requirements.
    per_req_unchecked_count: dict[str, int] = {}
    per_req_unchecked_max = 3

    # Post-patch build verification: on NEW compile/collection errors the
    # pipeline re-opens patch planning (CLOSED) with the errors fed back via
    # memory.build_error_feedback. Capped at patch_verify_rounds_max.
    patch_verify_rounds_used = 0
    patch_verify_rounds_max = 2
    # LTM/custom-rule retrieval for planning is expensive; run it once on the
    # first CLOSED entry and reuse the cached blocks on repatch rounds.
    planner_ltm_loaded = False
    # Every filepath any plan round intended to touch — used so the final
    # git-diff promotes ALL created files (a repatch overwrites memory.patch_plan
    # with the fixup plan, which would otherwise drop first-round new files).
    all_planned_files: set[str] = set()
    # Per-round build verification records, written to build_verification.json.
    build_verify_log: list[dict[str, Any]] = []
    # Consecutive deep-search failure counter per requirement id.
    # Prevents infinite retry when a requirement keeps failing before verdict is set.
    _ds_fail_counts: dict[str, int] = {}
    _DS_FAIL_MAX = 3

    # Dynamic grounding (phase 26): opt-in, runs at most once per case at the
    # evidence stage (before the closure-checker LLM). Tracks whether the single
    # pass has been consumed, plus the per-requirement reachability notes it
    # produced (injected into the closure-checker as a soft consistency input).
    _dynamic_grounding_done = False
    dynamic_notes: list[str] = []

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
            # Inject the rendered SharedWorkingMemory section (LTM summaries,
            # custom repair discipline, build-error feedback) so the deep-search
            # agent actually sees the same context the orchestrator gathered.
            working_memory_block = memory.format_for_prompt()
            print(
                f"[orchestrator] Dispatching deep-search for {target.id}: "
                f"{target.text[:80]!r}",
                flush=True,
            )
            try:
                report = await _run_deep_search_async(
                    todo_task, current_evidence, repo_dir,
                    working_memory_block=working_memory_block,
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

            # Per-requirement stall breaker: after persisting, check if the
            # requirement is STILL UNCHECKED (handler may have silently rejected
            # the write due to empty evidence_locations). Track consecutive stalls
            # and force-write after per_req_unchecked_max attempts.
            current_evidence_check = get_submitted_evidence()
            target_after = None
            if current_evidence_check:
                for r in current_evidence_check.requirements:
                    if r.id == target.id:
                        target_after = r
                        break
            if target_after and target_after.verdict == "UNCHECKED":
                per_req_unchecked_count[target.id] = (
                    per_req_unchecked_count.get(target.id, 0) + 1
                )
                if per_req_unchecked_count[target.id] >= per_req_unchecked_max:
                    print(
                        f"[orchestrator] {target.id} still UNCHECKED after "
                        f"{per_req_unchecked_count[target.id]} iterations "
                        f"(verdict write likely rejected) — forcing directly.",
                        flush=True,
                    )
                    target_after.verdict = report.requirement_verdict or "TO_BE_PARTIAL"
                    target_after.findings = report.requirement_findings or (
                        "[forced after repeated stall]"
                    )
                    target_after.evidence_locations = list(
                        report.requirement_evidence_locations
                        or report.exact_code_regions
                        or []
                    )
            else:
                per_req_unchecked_count.pop(target.id, None)

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
                for rid in bad_attribution:
                    reset_requirement_for_rework(
                        rid,
                        audit_feedback="Attribution check failed: non-compliant verdict "
                        "requires valid evidence_locations (path:LINE or path:LINE-LINE).",
                    )
                memory.record_action(
                    phase="evidence-refining",
                    outcome=f"attribution_failed:{len(bad_attribution)}_missing_locations",
                )
                assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                state = PipelineState.UNDER_SPECIFIED
                continue

            # Consistency-anchor format check (phase 23). Each anchor must
            # split into LHS/RHS as 'path:locator <-> path:locator'.
            anchor_format_bad = check_consistency_anchors_format(current_evidence)
            if anchor_format_bad and not budget.is_exhausted():
                print(
                    f"[orchestrator] consistency-anchor format check failed: "
                    f"{anchor_format_bad[:3]}. Returning to deep-search.",
                    flush=True,
                )
                memory.record_action(
                    phase="evidence-refining",
                    outcome=f"anchor_format_failed:{len(anchor_format_bad)}_bad",
                )
                assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                state = PipelineState.UNDER_SPECIFIED
                continue

            # Consistency-anchor factual check (phase 23). Pure file/grep —
            # no LLM. Failures are routed back to the requirement that owns
            # the LHS path so deep-search can re-investigate.
            anchor_failures = check_consistency_anchors(current_evidence, repo_dir)
            if anchor_failures and not budget.is_exhausted():
                # Group by owning requirement; reset each into rework with the
                # specific failure messages as audit_feedback.
                per_req: dict[str, list[str]] = {}
                for f in anchor_failures:
                    per_req.setdefault(f.requirement_id, []).append(f.render())
                reset_ids: list[str] = []
                for rid, lines in per_req.items():
                    if rid == "<global>":
                        continue
                    feedback = (
                        "Consistency-anchor code gate found endpoint(s) that "
                        "do not resolve in the repository:\n"
                        + "\n".join(f"  - {line}" for line in lines)
                        + "\n\nRe-investigate: either correct the anchor "
                        "(both endpoints must be agent-visible files; line "
                        "ranges and symbol names must exist) or, if the "
                        "endpoint truly should not exist, encode that as a "
                        "TO_BE_MISSING verdict instead of an anchor."
                    )
                    if reset_requirement_for_rework(rid, audit_feedback=feedback):
                        reset_ids.append(rid)
                # Global failures (no owning requirement) — log but do not
                # block; they will resurface next round if anchors stay broken.
                global_lines = per_req.get("<global>", [])
                if global_lines:
                    print(
                        "[orchestrator] anchor failures without owning req: "
                        + "; ".join(global_lines[:3]),
                        flush=True,
                    )
                if reset_ids:
                    print(
                        f"[orchestrator] consistency-anchor factual check → "
                        f"reset {reset_ids}; returning to deep-search.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=f"anchor_factual_failed:{len(reset_ids)}_reqs",
                    )
                    assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                    state = PipelineState.UNDER_SPECIFIED
                    continue

            # ── Static grounding gate (phase 25, ③ Correct attribution) ──
            # Deterministic code/grep/AST check that every cited region,
            # suspect symbol, findings snippet, call-chain edge, symptom symbol
            # and missing_element is actually grounded in the repository. This
            # LOWERS the closure-checker's old factual audit into code. Definite
            # refutations reset the owning requirement; global-card failures are
            # attributed back to a req (path/token/scoped) or fall back to a
            # whole-pipeline UNDER_SPECIFIED bounce when truly <global>.
            grounding_failures = run_static_grounding(current_evidence, repo_dir)
            if grounding_failures and not budget.is_exhausted():
                per_req_g: dict[str, list[str]] = {}
                global_lines_g: list[str] = []
                for gf in grounding_failures:
                    if gf.requirement_id == "<global>":
                        global_lines_g.append(gf.render())
                    else:
                        per_req_g.setdefault(gf.requirement_id, []).append(gf.render())
                reset_ids_g: list[str] = []
                for rid, lines in per_req_g.items():
                    feedback = (
                        "Static grounding gate found evidence that does NOT "
                        "resolve in the repository:\n"
                        + "\n".join(f"  - {line}" for line in lines)
                        + "\n\nRe-investigate: open the files yourself and either "
                        "correct the cited regions/symbols/findings to match the "
                        "actual code, or change the verdict if the code does not "
                        "support it."
                    )
                    if reset_requirement_for_rework(rid, audit_feedback=feedback):
                        reset_ids_g.append(rid)
                if reset_ids_g:
                    print(
                        f"[orchestrator] static-grounding gate → reset "
                        f"{reset_ids_g}; returning to deep-search.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=f"grounding_failed:{len(reset_ids_g)}_reqs",
                    )
                    assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                    state = PipelineState.UNDER_SPECIFIED
                    continue
                if global_lines_g:
                    # Unattributable failures: 3-tier attribution already missed,
                    # so there is no single requirement to reset. Bouncing the
                    # whole pipeline here would deadloop when no requirement is
                    # UNCHECKED (UNDER_SPECIFIED returns immediately, the gate
                    # re-fails, budget never moves). Mirror the existing <global>
                    # anchor precedent: surface them for the next deep-search
                    # round via memory, but do NOT block closure.
                    memory.build_error_feedback = (
                        (memory.build_error_feedback + "\n\n"
                         if memory.build_error_feedback else "")
                        + "Static grounding gate (unattributed global fields):\n"
                        + "\n".join(f"  - {line}" for line in global_lines_g)
                    )
                    print(
                        "[orchestrator] static-grounding gate: unattributable "
                        f"<global> failures (non-blocking): {global_lines_g[:3]}",
                        flush=True,
                    )
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=f"grounding_global_failed:{len(global_lines_g)}",
                    )

            # ── Dynamic grounding gate (phase 26, ③ runtime layer) ───────
            # Opt-in, at most ONCE per case. Reproduces the bug on base_commit,
            # applies the symptom gate, and mechanically matches the observed
            # failure path against each requirement's cited regions. NEVER
            # gates flow on its own (no reset): dynamic_reached is positive
            # confidence context; dynamic_not_reached is a soft consistency
            # note for the closure-checker; unverifiable is a no-op. The static
            # results above stand regardless. Working tree is restored inside
            # run_dynamic_grounding (base_commit hygiene).
            if not _dynamic_grounding_done:
                _dynamic_grounding_done = True
                try:
                    dyn_results = await run_dynamic_grounding(
                        current_evidence,
                        repo_dir,
                        problem_statement=memory.issue_context,
                    )
                except Exception as exc:
                    dyn_results = []
                    print(
                        "[orchestrator] dynamic-grounding raised "
                        f"{type(exc).__name__}: {exc} — treating as unverifiable",
                        flush=True,
                    )
                reached = [d for d in dyn_results if d.grounded_by == "dynamic_reached"]
                not_reached = [
                    d for d in dyn_results if d.grounded_by == "dynamic_not_reached"
                ]
                if reached:
                    pos_block = "\n".join(f"  - {d.render()}" for d in reached)
                    memory.build_error_feedback = (
                        (memory.build_error_feedback + "\n\n"
                         if memory.build_error_feedback else "")
                        + "Dynamic grounding (runtime confidence — failure path "
                        "traversed these cited locations):\n" + pos_block
                    )
                    print(
                        f"[orchestrator] dynamic-grounding: {len(reached)} req(s) "
                        "on reproduced failure path (positive context).",
                        flush=True,
                    )
                # not_reached notes are injected into the closure-checker as a
                # soft consistency input (never auto-reset — repro may be
                # incomplete).
                for d in not_reached:
                    dynamic_notes.append(d.render())
                if dyn_results:
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=(
                            f"dynamic_grounding:reached={len(reached)},"
                            f"not_reached={len(not_reached)}"
                        ),
                    )

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
                        dynamic_notes=dynamic_notes,
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
                    # Rework path (phase 25): derive per-requirement specs from
                    # the closure-checker's dimension findings.  sufficiency
                    # FAIL → deepen (full reset); consistency / prescriptive
                    # FAIL → reconcile (cross-req full reset or findings-only).
                    rework_specs = _derive_rework_specs(verdict)
                    # Req ids cited in missing/suggested_tasks but absent from
                    # the dimension findings (e.g. coverage-fail synthesized ids)
                    # have no spec → fall back to a deepen full reset.
                    cited_req_ids = _extract_req_ids(
                        verdict.missing + verdict.suggested_tasks
                    )
                    for rid in cited_req_ids:
                        if rid not in rework_specs:
                            rework_specs[rid] = RworkSpec(
                                "deepen", None,
                                "Flagged for rework without a specific dimension "
                                "finding; re-investigate this requirement fully.",
                            )

                    conflict_req_ids = list(rework_specs.keys())
                    if rework_rounds_used < rework_rounds_max and conflict_req_ids:
                        per_req_feedback = _build_per_req_audit_feedback(
                            verdict, conflict_req_ids,
                        )
                        reset_ids: list[str] = []
                        for rid in conflict_req_ids:
                            spec = rework_specs[rid]
                            combined_feedback = (
                                f"[operator={spec.operator}] {spec.feedback}\n\n"
                                + per_req_feedback.get(rid, "")
                            )
                            if reset_requirement_for_rework(
                                rid,
                                audit_feedback=combined_feedback,
                                fields_to_reset=spec.fields_to_reset,
                            ):
                                reset_ids.append(rid)
                    else:
                        reset_ids = []

                    if reset_ids:
                        rework_rounds_used += 1
                        scope_note = ", ".join(
                            f"{rid}:{rework_specs[rid].operator}="
                            f"{'findings' if rework_specs[rid].fields_to_reset == {'findings'} else 'full'}"
                            for rid in reset_ids
                        )
                        print(
                            "[orchestrator] EVIDENCE_MISSING → rework: "
                            f"re-opened {reset_ids} [{scope_note}] "
                            f"(round {rework_rounds_used}/{rework_rounds_max})",
                            flush=True,
                        )
                        operators = ",".join(
                            sorted({rework_specs[rid].operator for rid in reset_ids})
                        )
                        memory.record_action(
                            phase="closure-check",
                            subagent="closure-checker",
                            outcome=(
                                f"rework:{operators}:reopen={len(reset_ids)}_reqs:"
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
            # Long-term memory injection for patch planning: top-3 with full
            # fix_experience text, overwrites the lighter under-specified
            # block that was set at startup.  Reference experience is
            # marked in the prompt itself as "code wins on conflict" so the
            # planner does not blindly transplant a fix from another repo.
            #
            # Run retrieval only once: on repatch rounds (re-entry from
            # PATCH_VERIFYING) the cached blocks on memory are reused and the
            # build_error_feedback drives the new plan instead.
            if not planner_ltm_loaded:
                planner_summaries, planner_experiences = await _progressive_retrieve_ltm(
                    stage="patch_planning",
                    query=ltm_query,
                    output_dir=output_dir,
                    summary_top_k=5,
                    detail_top_k=3,
                )
                planner_block = format_experiences_for_prompt(
                    planner_experiences, include_fix=True,
                )
                planner_custom_block = await _route_and_match_custom_rules(
                    stage="patch_planning",
                    query=custom_route_query or ltm_query,
                    output_dir=output_dir,
                )
                memory.ltm_search_summaries = planner_summaries
                memory.ltm_reference_block = planner_block
                memory.custom_repair_block = planner_custom_block
                planner_ltm_loaded = True
            else:
                print(
                    "[orchestrator] repatch re-plan: reusing cached LTM blocks, "
                    "build errors drive the new plan.",
                    flush=True,
                )

            print("[orchestrator] Dispatching patch-planner...", flush=True)
            plan = await _run_patch_planner_async(memory, repo_dir)
            memory.record_action(
                phase="patch-planning",
                subagent="patch-planner",
                outcome=f"{len(plan.edits)}_files_planned",
            )

            assert is_valid_transition(state, PipelineState.PATCH_PLANNING)
            state = PipelineState.PATCH_PLANNING

        # ── PatchPlanning: dispatch patch-generator ───────────────────
        elif state == PipelineState.PATCH_PLANNING:
            # Accumulate planned files across rounds so the final git-diff
            # promotes every created file even after a repatch overwrites
            # memory.patch_plan with the fixup plan.
            if memory.patch_plan is not None:
                all_planned_files.update(e.filepath for e in memory.patch_plan.edits)

            print("[orchestrator] Dispatching patch-generator...", flush=True)
            success = await _run_patch_generator_async(memory, repo_dir, output_dir)
            if success:
                memory.record_action(
                    phase="patch-generation",
                    subagent="patch-generator",
                    outcome="PATCH_APPLIED",
                )
                # Defer the final PATCH_SUCCESS verdict to the build gate.
                assert is_valid_transition(state, PipelineState.PATCH_VERIFYING)
                state = PipelineState.PATCH_VERIFYING
            else:
                memory.record_action(
                    phase="patch-generation",
                    subagent="patch-generator",
                    outcome="PATCH_FAILED",
                )
                patch_outcome = "PATCH_FAILED"
                assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                state = PipelineState.PATCH_FAILED

        # ── PatchVerifying: deterministic post-patch build gate ────────
        elif state == PipelineState.PATCH_VERIFYING:
            system = detect_build_system(repo_dir)
            print(
                f"[build-verify] build system detected: {system}",
                flush=True,
            )

            if system in ("node", "unknown"):
                print(
                    f"[build-verify] no compile step for '{system}'; skipping "
                    "build gate (accepting patch).",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome=f"skipped:{system}",
                )
                build_verify_log.append(
                    {"round": patch_verify_rounds_used, "system": system,
                     "skipped": True, "ok": True}
                )
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            post = run_build_check(repo_dir, system)
            print(
                f"[build-verify] post-patch: ok={post.ok} "
                f"errors={len(post.errors)} timed_out={post.timed_out} "
                f"cmd='{post.command}'",
                flush=True,
            )

            # Rename-residue gate (phase 23): independent of build outcome.
            # Old symbols left behind by an under-propagated rename frequently
            # compile fine yet break grep-visible callers — the build gate
            # alone misses them. We diff the working tree against HEAD; the
            # repo was hard-reset to base_commit at startup, so HEAD == base.
            residues = check_rename_residue(repo_dir, base_commit=None)
            if residues:
                print(
                    f"[build-verify] rename-residue gate: "
                    f"{len(residues)} unupdated old-symbol references.",
                    flush=True,
                )
            else:
                print("[build-verify] rename-residue gate: clean.", flush=True)

            # Toolchain unavailable (e.g. no `go` on this host) → the build gate
            # could not run and has NO opinion on the patch. We must NOT treat
            # this as a verified pass (the old code did: rc=127 produced empty
            # errors that baseline-subtraction cancelled to "no new errors",
            # silently green-lighting every Go patch on a host without Go).
            # The rename-residue gate is git-only and still valid, so honour it;
            # the build verdict itself is recorded as BUILD_UNVERIFIABLE. The
            # patch is still written — the real arbiter is the docker eval.
            if post.unverifiable and not residues:
                print(
                    "[build-verify] BUILD_UNVERIFIABLE: toolchain unavailable or "
                    f"build could not be attributed (cmd='{post.command}'); "
                    "skipping baseline diff and accepting patch unverified. "
                    "Run the docker eval for a real build/test verdict.",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome="unverifiable",
                )
                build_verify_log.append(
                    {"round": patch_verify_rounds_used, "system": system,
                     "ok": False, "unverifiable": True,
                     "command": post.command, "timed_out": post.timed_out}
                )
                patch_outcome = "BUILD_UNVERIFIABLE"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            if post.ok and not residues:
                memory.record_action(phase="build-verify", outcome="ok")
                build_verify_log.append(
                    {"round": patch_verify_rounds_used, "system": system,
                     "ok": True, "command": post.command}
                )
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            # Build failed — distinguish patch-induced from pre-existing.
            if post.ok:
                baseline = None
                new_errors = []
            else:
                baseline = _compute_baseline_build(repo_dir, system)
                new_errors = diff_new_errors(baseline, post)
                print(
                    f"[build-verify] new (patch-induced) errors: {len(new_errors)} "
                    f"(baseline_errors="
                    f"{len(baseline.errors) if baseline else 'n/a'})",
                    flush=True,
                )

            # Combine residue records with build errors so the repatch loop
            # sees one unified feedback channel.
            combined_errors = list(new_errors) + list(residues)
            build_verify_log.append(
                {
                    "round": patch_verify_rounds_used,
                    "system": system,
                    "ok": post.ok,
                    "command": post.command,
                    "timed_out": post.timed_out,
                    "baseline_computed": baseline is not None,
                    "new_errors": [
                        {"file": e.file, "line": e.line, "message": e.message}
                        for e in new_errors
                    ],
                    "rename_residues": [
                        {"file": e.file, "line": e.line, "message": e.message}
                        for e in residues
                    ],
                }
            )

            if not combined_errors:
                # All failures pre-existed at base_commit — not the patch's
                # fault. Accept the patch.
                print(
                    "[build-verify] all build errors pre-existed at base "
                    "and no rename residues; accepting patch.",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome="ok_preexisting_only",
                )
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            if patch_verify_rounds_used < patch_verify_rounds_max:
                patch_verify_rounds_used += 1
                feedback_parts: list[str] = []
                if new_errors:
                    feedback_parts.append(render_errors_for_feedback(new_errors))
                if residues:
                    feedback_parts.append(render_residue_for_feedback(residues))
                memory.build_error_feedback = "\n\n".join(p for p in feedback_parts if p)
                print(
                    "[build-verify] BUILD_FAILED → repatch: re-opening planning "
                    f"(round {patch_verify_rounds_used}/{patch_verify_rounds_max}) "
                    f"with {len(new_errors)} build errors and "
                    f"{len(residues)} rename residues.",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome=(
                        f"repatch:round={patch_verify_rounds_used}/"
                        f"{patch_verify_rounds_max}:"
                        f"{len(new_errors)}_errors+{len(residues)}_residues"
                    ),
                )
                assert is_valid_transition(state, PipelineState.CLOSED)
                state = PipelineState.CLOSED
            else:
                print(
                    "[build-verify] BUILD_FAILED and repatch budget exhausted; "
                    "terminating as PATCH_FAILED.",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome="BUILD_FAILED_terminal",
                )
                patch_outcome = "BUILD_FAILED"
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

    # Persist per-round build verification diagnostics.
    if build_verify_log:
        bv_path = output_dir / "build_verification.json"
        bv_path.write_text(
            json.dumps(build_verify_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[orchestrator] Build verification log saved -> {bv_path}", flush=True)

    # Collect git diff \u2014 pass planned files so newly-created files are
    # promoted from untracked to intent-to-add and surface in the diff.
    # Use the UNION of every file any plan round intended to touch so a repatch
    # (which overwrote memory.patch_plan with the fixup plan) does not drop a
    # file the first round created.  The narrower coverage check below uses the
    # final plan only, to avoid false downgrades from first-round files that
    # turned out unnecessary.
    final_plan_files: list[str] = []
    if wm.patch_plan is not None:
        final_plan_files = [edit.filepath for edit in wm.patch_plan.edits]
    diff_planned_files = sorted(all_planned_files | set(final_plan_files))
    diff_text = _collect_git_diff(repo_dir, planned_files=diff_planned_files)
    if diff_text.startswith("\ufeff"):
        diff_text = diff_text.lstrip("\ufeff")

    # Plan-coverage check: every file in the FINAL plan must appear in the diff.
    # If the generator silently dropped an edit (e.g. SEARCH mismatch it
    # failed to recover from), the outcome is a partial patch; downgrade
    # PATCH_SUCCESS so downstream eval sees the real state.
    missing_from_diff = _verify_plan_coverage(diff_text, final_plan_files)
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
    problem_statement: str = "",
) -> Path:
    """Synchronous entry-point. Returns the path to evidence.json."""
    return asyncio.run(
        run_pipeline(
            issue_id=issue_id,
            repo_dir=repo_dir,
            artifact_text=artifact_text,
            output_dir=output_dir,
            problem_statement=problem_statement,
        )
    )
