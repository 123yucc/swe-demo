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
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents._model_infra import ModelInfrastructureError

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


def _semantic_field_feedback(conflicting_field: str | None) -> str:
    """Translate closure's legacy field labels to fields deep-search can write."""
    if conflicting_field == "repair_targets":
        return (
            "Closure calls this a repair_targets gap, but deep-search cannot "
            "write parser-owned symptom.repair_targets. Satisfy the gap by "
            "populating concrete writable localization fields: exact_code_regions "
            "for edit/import sites, must_co_edit_relations for files/symbols that "
            "must change together, and dependency_propagation for config/import/"
            "call-chain propagation. Enumerate exact file paths and line/symbol "
            "anchors instead of repeating a generic need for a repair target. "
            "If the gap involves moving, extracting, centralizing, or splitting "
            "logic, choose ONE concrete steady-state strategy and record it "
            "prescriptively in must_co_edit_relations/dependency_propagation. "
            "Do not leave an either/or plan such as 'update all imports OR keep "
            "a shim'. State the canonical owner, whether old locations are "
            "deleted, delegated, or re-exported, and the caller/import surface "
            "covered by that strategy. If the requirement or interface block "
            "names an explicit new Path/Name, use that exact path/name as the "
            "canonical target; do not weaken it to 'or similarly named'. Treat "
            "tests as compatibility evidence unless a language-specific rename "
            "rule explicitly makes base tests part of the production edit "
            "surface."
        )
    if conflicting_field == "missing_elements":
        return (
            "Closure calls this a missing_elements gap, but deep-search cannot "
            "write parser-owned constraint.missing_elements_to_implement. Satisfy "
            "the gap by documenting the missing implementation in findings and by "
            "populating exact_code_regions, similar_implementation_patterns, "
            "must_co_edit_relations, and dependency_propagation with concrete "
            "implementation/integration anchors. If more than one implementation "
            "strategy is possible, select ONE codebase-consistent strategy and "
            "record it as a concrete edit/delegation/import propagation contract; "
            "do not preserve unresolved alternatives for patch planning. If the "
            "interface block names an explicit Path/Name, preserve that exact "
            "Path/Name as the missing implementation target."
        )
    if conflicting_field == "evidence_locations":
        return (
            "Return line-numbered requirement_evidence_locations for existing "
            "implementation or integration points. For a new file/symbol, cite "
            "the existing file/line where it is imported, called, configured, "
            "or otherwise mounted."
        )
    return (
        "Use writable DeepSearchReport fields that match this gap: findings, "
        "requirement_evidence_locations, exact_code_regions, must_co_edit_relations, "
        "dependency_propagation, behavioral_constraints, semantic_boundaries, "
        "backward_compatibility, and similar_implementation_patterns."
    )


def _eligible_closure_rework_ids(
    conflict_req_ids: list[str],
    frozen_req_ids: set[str],
    rework_rounds_by_req: dict[str, int],
    per_req_rework_rounds_max: int,
) -> tuple[list[str], list[str], list[str]]:
    """Return (eligible, frozen_excluded, capped_excluded) for closure rework."""
    frozen = [rid for rid in conflict_req_ids if rid in frozen_req_ids]
    not_frozen = [rid for rid in conflict_req_ids if rid not in frozen_req_ids]
    capped = [
        rid for rid in not_frozen
        if rework_rounds_by_req.get(rid, 0) >= per_req_rework_rounds_max
    ]
    eligible = [
        rid for rid in not_frozen
        if rework_rounds_by_req.get(rid, 0) < per_req_rework_rounds_max
    ]
    return eligible, frozen, capped


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

    for gap in verdict.shared_fact_gaps:
        entry = (gap.fact or "").strip()
        if gap.suggested_anchor:
            entry = (
                f"{entry}\nSuggested anchor: {gap.suggested_anchor}"
                if entry else f"Suggested anchor: {gap.suggested_anchor}"
            )
        if not entry:
            continue
        targets = [rid for rid in gap.requirement_ids if rid in per_req_entries]
        if not targets:
            targets = list(per_req_entries.keys())
        for rid in targets:
            per_req_entries[rid].append(f"[shared_fact_gap] {entry}")

    out: dict[str, str] = {}
    for rid, entries in per_req_entries.items():
        body = "\n".join(entries) if entries else "(no entry cited this req)"
        out[rid] = (
            f"Closure-checker rationale:\n{rationale}\n\n"
            f"Audit items concerning {rid}:\n{body}\n\n"
            "Instruction: on this rework iteration you MUST reconsider the "
            "prior verdict and localization. Read the cited code regions "
            "yourself, and if you still reach the previous verdict, explicitly "
            "cite the code lines that refute the audit item above. Do not "
            "repeat the same reasoning path that was driven by the prior "
            "verdict. If the audit item asks for repair_targets or "
            "missing_elements, translate that into writable DeepSearchReport "
            "fields: exact_code_regions, requirement_evidence_locations, "
            "must_co_edit_relations, dependency_propagation, and "
            "similar_implementation_patterns. When the feedback concerns a "
            "move/extract/centralize/split refactor, choose one steady-state "
            "strategy: name the canonical owner, name any old-location shim/"
            "delegate/re-export behavior, and list the import/caller surface "
            "that follows that strategy. Do not answer with unresolved "
            "alternatives such as 'either update callers or keep a shim'. If "
            "the requirement/interface text provides an exact new path or "
            "symbol name, keep it exact. Do not generalize it to a sibling or "
            "'similarly named' target. Treat tests as compatibility evidence "
            "rather than required edit targets unless a language-specific "
            "rename rule explicitly says otherwise."
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
                f"(localization / constraints) for {field_label}.\n"
                f"{_semantic_field_feedback(finding.conflicting_field)}"
            )
            targets = finding.requirement_ids or []
            for rid in targets:
                _add(rid, RworkSpec("deepen", None, feedback))
        elif finding.dimension == "consistency":
            if verdict.conflicts:
                # Structured conflict edges below choose the weaker/recheck
                # side. Do not reopen every id from the legacy flat finding.
                continue
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

    for edge in verdict.conflicts:
        targets = []
        if edge.recommended_recheck_side in {"left", "both"}:
            targets.append(edge.left_requirement_id)
        if edge.recommended_recheck_side in {"right", "both"}:
            targets.append(edge.right_requirement_id)
        feedback = (
            f"Structured conflict on {edge.conflicting_field}: {edge.explanation}. "
            f"Shared evidence: {', '.join(edge.shared_evidence)}."
        )
        for rid in targets:
            _add(rid, RworkSpec("reconcile", None, feedback))

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
from src.agents.deep_search_agent import _run_adaptive_deep_search_async, _run_deep_search_async
from src.agents.call_metrics import model_label, write_event
from src.agents.ltm_agent import run_agentic_ltm_retrieval
from src.agents.parser_agent import _run_parser_async
from src.agents.patch_generator_agent import (
    PatchGeneratorInfraError,
    _run_patch_generator_async,
)
from src.agents.patch_planner_agent import _run_patch_planner_async, _split_heavy_edits
from src.agents._structured import close_structured_clients
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
from src.models.patch import FileEditPlan, PatchPlan
from src.models.verdict import ClosureVerdict
from src.orchestrator.audit import build_audit_manifest
from src.models.audit import DimensionFinding
from src.orchestrator.artifact_verify import (
    ArtifactFinding,
    render_artifact_feedback,
    verify_patch_artifacts,
)
from src.orchestrator.build_verify import (
    BuildCheckResult,
    BuildError,
    changed_python_production_files,
    changed_go_packages,
    detect_build_system,
    diff_new_errors,
    render_errors_for_feedback,
    run_build_check,
)
from src.orchestrator.consistency_checks import (
    check_consistency_anchors,
    check_config_entry_shape,
    check_contract_drift,
    check_go_unexport_consistency,
    check_parallel_impl_consistency,
    check_python_moved_class_dunder_methods,
    check_python_config_subscript_fallback,
    check_python_helper_api_usage,
    check_python_noniterable_class_loop,
    check_removed_symbol_test_refs,
    check_rename_residue,
    check_undefined_config_symbol,
    is_test_file,
    render_config_entry_shape_for_feedback,
    render_contract_drift_for_feedback,
    render_go_unexport_for_feedback,
    render_parallel_impl_for_feedback,
    render_python_moved_class_dunder_methods_for_feedback,
    render_python_config_subscript_fallback_for_feedback,
    render_python_helper_api_usage_for_feedback,
    render_python_noniterable_class_loop_for_feedback,
    render_removed_symbol_test_refs_for_feedback,
    render_residue_for_feedback,
    render_undefined_config_symbol_for_feedback,
    revert_test_file_edits,
)
from src.orchestrator.grounding import run_static_grounding
from src.orchestrator.guards import (
    DeepSearchBudget,
    check_consistency_anchors_format,
    check_correct_attribution,
    check_plan_covers_violations,
    check_structural_invariants,
    check_sufficiency,
    render_plan_coverage_feedback,
)
from src.orchestrator.states import (
    PipelineState,
    is_valid_transition,
)
from src.orchestrator.work_packages import build_anchor_index, create_work_packages
from src.tools.ingestion_tools import (
    DEEP_SEARCH_OWNED_FIELDS,
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
        if is_test_file(norm):
            continue
        if norm not in diff_paths:
            missing.append(norm)
    return missing


def _compute_baseline_build(
    repo_dir: Path,
    system: str,
    python_targets: list[str] | None = None,
    go_targets: list[str] | None = None,
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
        baseline = run_build_check(
            repo_dir,
            system,
            python_targets=python_targets,
            go_targets=go_targets,
        )
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


_GO_SIG_ERROR_RE = re.compile(
    r"assignment mismatch|unknown field|undefined|not enough arguments|"
    r"too many arguments|cannot use"
)
_GO_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_GO_UNDEFINED_RE = re.compile(r"undefined:\s+([A-Za-z_]\w*)\b")
_GO_UNDEFINED_QUALIFIED_RE = re.compile(r"undefined:\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)")
_GO_UNKNOWN_FIELD_RE = re.compile(r"unknown field\s+([A-Za-z_]\w*)\s+in struct literal")
_GO_CALL_QUALIFIED_RE = re.compile(
    r"(?:in call to|cannot use)\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b"
)
_GO_TYPE_MEMBER_MISSING_RE = re.compile(
    r"type\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s+has no field or method\s+([A-Za-z_]\w*)"
)
_GO_LOCAL_IMPORT_ERROR_RE = re.compile(
    r"(?:package|module provides package)\s+(?P<path>[A-Za-z0-9_./-]*internal/[A-Za-z0-9_./-]+)"
)
_GO_IMPORT_RE = re.compile(
    r'(?m)^\s*(?:(?P<alias>[A-Za-z_]\w*)\s+)?'
    r'"(?P<path>[^"]+)"'
)
_GO_SINGLE_IMPORT_RE = re.compile(
    r'(?m)^\s*import\s+(?:(?P<alias>[A-Za-z_]\w*)\s+)?'
    r'"(?P<path>[^"]+)"'
)
_GO_EXPORTED_DEF_RE = re.compile(
    r"^\s*(?:func\s+(?:\([^)]*\)\s*)?(?P<func>[A-Z]\w*)\s*\(|"
    r"type\s+(?P<type>[A-Z]\w*)\b|"
    r"(?:const|var)\s+(?P<single>[A-Z]\w*)\b|"
    r"(?P<block>[A-Z]\w*)\s*(?:=|[A-Za-z_][\w.\[\]*{}]*))"
)
_REMOVED_SYMBOL_NAME_RE = re.compile(
    r"(?:definition of|surface/member named) '([^']+)'"
)
_UNDEFINED_CONFIG_SYMBOL_RE = re.compile(r"undefined config symbol:\s+'([^']+)'")


def _exact_symbols_from_build_error(message: str) -> list[str]:
    symbols: list[str] = []
    qualified_aliases: set[str] = set()
    for match in _UNDEFINED_CONFIG_SYMBOL_RE.findall(message or ""):
        if match not in symbols:
            symbols.append(match)
    for alias, match in _GO_UNDEFINED_QUALIFIED_RE.findall(message or ""):
        qualified_aliases.add(alias)
        if match not in symbols:
            symbols.append(match)
    for _, match in _GO_CALL_QUALIFIED_RE.findall(message or ""):
        if match not in symbols:
            symbols.append(match)
    for _, _, match in _GO_TYPE_MEMBER_MISSING_RE.findall(message or ""):
        if match not in symbols:
            symbols.append(match)
    for match in _GO_UNDEFINED_RE.findall(message or ""):
        if "." in match or match in symbols or match in qualified_aliases:
            continue
        symbols.append(match)
    for match in _GO_UNKNOWN_FIELD_RE.findall(message or ""):
        if match not in symbols:
            symbols.append(match)
    return symbols


def _go_module_path(repo_dir: Path) -> str:
    try:
        text = (repo_dir / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("module "):
            return line.split(None, 1)[1].strip()
    return ""


def _go_import_aliases(file_text: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in list(_GO_IMPORT_RE.finditer(file_text)) + list(_GO_SINGLE_IMPORT_RE.finditer(file_text)):
        path = match.group("path")
        alias = match.group("alias") or path.rsplit("/", 1)[-1]
        if alias in {".", "_"}:
            continue
        aliases[alias] = path
    return aliases


def _go_package_export_summary(repo_dir: Path, package_dir: Path, limit: int = 80) -> list[str]:
    exports: list[str] = []
    seen: set[str] = set()
    if not package_dir.exists() or not package_dir.is_dir():
        return exports
    for path in sorted(package_dir.glob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            m = _GO_EXPORTED_DEF_RE.match(line)
            if not m:
                continue
            name = next((v for v in m.groupdict().values() if v), "")
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                rel = path.relative_to(repo_dir).as_posix()
            except ValueError:
                rel = path.as_posix()
            exports.append(f"  {rel}: {name} :: {stripped[:140]}")
            if len(exports) >= limit:
                return exports
    return exports


def _enrich_go_errors_with_package_exports(
    repo_dir: Path,
    errors: list[BuildError],
    limit_packages: int = 6,
) -> str:
    """Attach exported names for packages cited by ``undefined: pkg.X``."""
    module = _go_module_path(repo_dir)
    if not module:
        return ""
    package_requests: dict[tuple[str, str], set[str]] = {}
    file_cache: dict[str, str] = {}
    for err in errors:
        if not err.file.endswith(".go"):
            continue
        matches = _GO_UNDEFINED_QUALIFIED_RE.findall(err.message)
        if not matches:
            continue
        if err.file not in file_cache:
            try:
                file_cache[err.file] = (repo_dir / err.file).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                file_cache[err.file] = ""
        aliases = _go_import_aliases(file_cache[err.file])
        for alias, symbol in matches:
            import_path = aliases.get(alias)
            if not import_path or not import_path.startswith(module + "/"):
                continue
            package_requests.setdefault((alias, import_path), set()).add(symbol)

    blocks: list[str] = []
    for (alias, import_path), missing in list(package_requests.items())[:limit_packages]:
        rel_dir = import_path[len(module) + 1 :]
        exports = _go_package_export_summary(repo_dir, repo_dir / rel_dir)
        if not exports:
            continue
        blocks.append(
            f"Package {alias} ({import_path}) was referenced with missing "
            f"symbol(s): {', '.join(sorted(missing))}. Actual exported names "
            "currently present in that package:\n" + "\n".join(exports[:40])
        )
    if not blocks:
        return ""
    return (
        "Go package export context for undefined qualified symbols. Use these "
        "actual names or add an explicit compatibility alias in the defining "
        "package; do not invent analogous names:\n\n" + "\n\n".join(blocks)
    )


def _enrich_go_errors_with_module_import_paths(
    repo_dir: Path,
    errors: list[BuildError],
) -> str:
    """Render concrete module-qualified replacements for bad local imports."""
    module = _go_module_path(repo_dir)
    if not module:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for err in errors:
        for match in _GO_LOCAL_IMPORT_ERROR_RE.finditer(err.message):
            bad = match.group("path").strip().strip('"')
            idx = bad.find("internal/")
            if idx < 0:
                continue
            suffix = bad[idx:]
            desired = f"{module}/{suffix}"
            if desired in seen:
                continue
            seen.add(desired)
            exists_note = (
                "directory exists"
                if (repo_dir / suffix).is_dir()
                else "directory not found; verify the intended local package"
            )
            loc = f"{err.file}:{err.line}" if err.line is not None else err.file
            lines.append(
                f"- {loc}: {err.message}\n"
                f"  go.mod module is {module}; local internal package imports "
                f"must use {desired} ({exists_note}). Do not use bare "
                f"{suffix} or another repository's module prefix."
            )
    if not lines:
        return ""
    return "Go local import path correction:\n" + "\n".join(lines)


def _extract_python_symbol_block(source: str, symbol: str, max_lines: int = 140) -> str:
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not (
            re.match(rf"(?:async\s+)?def\s+{re.escape(symbol)}\s*\(", stripped)
            or re.match(rf"class\s+{re.escape(symbol)}\b", stripped)
            or re.match(rf"{re.escape(symbol)}\s*(?::[^=]+)?=", stripped)
        ):
            continue
        start = idx
        while start > 0 and lines[start - 1].lstrip().startswith("@"):
            start -= 1
        end = idx + 1
        while end < len(lines) and end - start < max_lines:
            nxt = lines[end]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip()
    return ""


def _enrich_removed_symbol_errors_with_base_definitions(
    repo_dir: Path,
    errors: list[BuildError],
    limit_symbols: int = 8,
) -> str:
    """Attach base-version definitions for removed-symbol static repairs."""
    blocks: list[str] = []
    seen: set[tuple[str, str]] = set()
    for err in errors:
        m = _REMOVED_SYMBOL_NAME_RE.search(err.message)
        if not m or not err.file:
            continue
        symbol = m.group(1)
        key = (err.file, symbol)
        if key in seen:
            continue
        seen.add(key)
        rc, base_text, _ = _run_git(repo_dir, "show", f"HEAD:{err.file}")
        if rc != 0 or not base_text:
            continue
        block = _extract_python_symbol_block(base_text, symbol) if err.file.endswith(".py") else ""
        if not block:
            lines = base_text.splitlines()
            for idx, line in enumerate(lines):
                if re.search(rf"\b{re.escape(symbol)}\b", line):
                    start = max(0, idx - 5)
                    end = min(len(lines), idx + 25)
                    block = "\n".join(lines[start:end]).rstrip()
                    break
        if not block:
            continue
        blocks.append(
            f"Base definition for {symbol} in {err.file}; restore a compatible "
            "production shim/API unless the requirement explicitly removes it:\n"
            f"```text\n{block}\n```"
        )
        if len(blocks) >= limit_symbols:
            break
    if not blocks:
        return ""
    return "Deleted-symbol base definitions for static repair:\n\n" + "\n\n".join(blocks)


def _enrich_go_errors_with_definitions(
    repo_dir: Path,
    errors: list[BuildError],
    limit: int = 8,
) -> str:
    """For Go build errors about signature/shape mismatches, grep the cited
    identifier's definition line and return a compact text block.

    Issue-013 shape: the generator wrote ``x, err := f()`` against a function
    returning a single value, and two repatch rounds repeated the mistake
    because the feedback never showed the real signature. Attaching the
    actual ``func``/``type``/field definition line gives the planner the
    ground truth it kept guessing at. Pure git grep — no LLM. Best-effort:
    any identifier we cannot resolve is silently skipped.
    """
    blocks: list[str] = []
    seen_idents: set[str] = set()
    for err in errors:
        if not _GO_SIG_ERROR_RE.search(err.message):
            continue
        # Pull candidate identifiers out of the error message; resolve the
        # first few that look like definitions somewhere in the tree.
        for ident in _GO_IDENT_RE.findall(err.message):
            if len(ident) < 3 or ident in seen_idents:
                continue
            if ident in {"assignment", "mismatch", "unknown", "field",
                         "undefined", "not", "enough", "arguments", "too",
                         "many", "cannot", "use", "type", "struct", "func",
                         "variable", "variables", "value", "values", "in",
                         "of", "the", "has", "no", "method", "literal"}:
                continue
            seen_idents.add(ident)
            pattern = (
                rf"(^|\s)(func\s+(\([^)]*\)\s*)?{re.escape(ident)}\s*\(|"
                rf"type\s+{re.escape(ident)}\b|"
                rf"{re.escape(ident)}\s+[\*\[\]\w.]+\s*(//.*)?$)"
            )
            rc, out, _ = _run_git(
                repo_dir, "grep", "-nE", "--no-color", pattern, "--", "*.go"
            )
            if rc != 0 or not out.strip():
                continue
            hit_lines = [l for l in out.splitlines() if l.strip()][:2]
            for hl in hit_lines:
                blocks.append(f"  {hl.strip()[:160]}")
            if len(blocks) >= limit:
                break
        if len(blocks) >= limit:
            break
    if not blocks:
        return ""
    return (
        "Actual definitions of the symbols in the errors above (match your "
        "call sites to these signatures exactly):\n" + "\n".join(blocks)
    )


def _go_local_import_path_to_dir(repo_dir: Path, import_path: str) -> Path | None:
    module = _go_module_path(repo_dir)
    if not module or not import_path.startswith(module + "/"):
        return None
    rel_dir = import_path[len(module) + 1 :].strip("/")
    if not rel_dir:
        return None
    package_dir = repo_dir / rel_dir
    if not package_dir.is_dir():
        return None
    return package_dir


def _go_package_definition_files(
    repo_dir: Path,
    package_dir: Path,
    symbol: str,
    *,
    limit: int = 3,
) -> list[str]:
    if not symbol:
        return []
    patterns = (
        rf"^\s*func\s+(?:\([^)]*\)\s*)?{re.escape(symbol)}\s*\(",
        rf"^\s*type\s+{re.escape(symbol)}\b",
        rf"^\s*(?:const|var)\s+{re.escape(symbol)}\b",
        rf"^\s*{re.escape(symbol)}\s*(?:=|[A-Za-z_][\w.\[\]*{{}}]*)",
    )
    hits: list[str] = []
    for path in sorted(package_dir.glob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(re.search(pattern, text, flags=re.MULTILINE) for pattern in patterns):
            continue
        try:
            rel = path.relative_to(repo_dir).as_posix()
        except ValueError:
            rel = path.as_posix()
        hits.append(rel)
        if len(hits) >= limit:
            break
    return hits


def _go_file_mentions_symbol(
    repo_dir: Path,
    relpath: str,
    symbol: str,
) -> bool:
    if not symbol:
        return False
    path = repo_dir / relpath
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(rf"\b{re.escape(symbol)}\b", text) is not None


def _pick_next_requirement(
    evidence: EvidenceCards,
    frozen_ids: set[str] | None = None,
) -> RequirementItem | None:
    """Return the first RequirementItem whose verdict is still UNCHECKED.

    frozen_ids: requirements whose stall-freeze prevented verified evidence;
    they keep whatever non-UNCHECKED verdict was force-written and must not be
    re-investigated even if the grounding gate later resets them to UNCHECKED.
    """
    for req in evidence.requirements:
        if req.verdict == "UNCHECKED":
            if frozen_ids and req.id in frozen_ids:
                continue
            return req
    return None


_DEEP_SEARCH_ITERATION_OUTCOME_RE = re.compile(r"^iter(\d+):")


def _restore_deep_search_iteration(saved: int, action_history: list) -> int:
    """Restore a monotonic deep-search count, including legacy checkpoints.

    Older checkpoints accidentally persisted ``budget.iterations`` while the
    budget class increments ``budget.iteration``.  Their saved value is usually
    zero even after many completed searches.  Recover the largest recorded
    iteration from action history so resuming such a checkpoint cannot silently
    grant another full budget.
    """
    restored = max(0, int(saved or 0))
    for event in action_history:
        if getattr(event, "subagent", "") != "deep-search":
            continue
        match = _DEEP_SEARCH_ITERATION_OUTCOME_RE.match(
            getattr(event, "outcome", "")
        )
        if match:
            restored = max(restored, int(match.group(1)))
    return restored


def _deep_search_iteration_limit(requirement_count: int) -> int:
    """Size the search budget so every parsed requirement can be inspected.

    The historical fixed limit of 30 made issues with more than 30 requirement
    fragments fail deterministically: the final fragments stayed UNCHECKED
    before the first closure pass.  Keep the established 30-iteration floor for
    ordinary issues, while reserving bounded headroom for closure-directed
    rework on larger issues.
    """
    count = max(0, int(requirement_count or 0))
    return max(30, count + 10)


def _should_run_compile_repair(
    rounds_used: int,
    rounds_max: int,
    current_error_count: int,
    previous_error_count: int | None,
) -> bool:
    """Grant one bonus round only when compile repair is nearly converged."""
    if rounds_used < rounds_max:
        return True
    return (
        rounds_used == rounds_max
        and 0 < current_error_count <= 3
        and previous_error_count is not None
        and current_error_count < previous_error_count
    )


# ── Phase-27 heuristic-gate plumbing ────────────────────────────────────────

def _gate_signature(err: BuildError) -> str:
    """Stable identity for a heuristic gate finding across repatch rounds."""
    return f"{err.file.replace(chr(92), '/').strip()}::{err.raw.strip()[:120]}"


def _partition_by_fuse(
    errors: list[BuildError],
    fed_back: set[str],
) -> tuple[list[BuildError], set[str]]:
    """Split *errors* into (active, downgraded-signatures).

    A finding whose signature was already fed back to the planner in a prior
    round and reappeared unchanged is downgraded to a warning: the planner saw
    it and deliberately kept the edit, so we stop blocking on it. This caps the
    worst-case cost of any heuristic false positive at a single extra repatch
    round and gives the model a "insist and pass" channel for legitimate edits.
    """
    active: list[BuildError] = []
    downgraded: set[str] = set()
    for err in errors:
        sig = _gate_signature(err)
        if sig in fed_back:
            downgraded.add(sig)
        else:
            active.append(err)
    return active, downgraded


def _record_fed_back(errors: list[BuildError], fed_back: set[str]) -> None:
    for err in errors:
        fed_back.add(_gate_signature(err))


def _errs_to_log(errors: list[BuildError]) -> list[dict[str, Any]]:
    return [
        {"file": e.file, "line": e.line, "message": e.message}
        for e in errors
    ]


def _python_module_to_source_path(repo_dir: Path, module: str) -> str | None:
    rel = Path(*module.split("."))
    py = repo_dir / f"{rel.as_posix()}.py"
    if py.is_file():
        return py.relative_to(repo_dir).as_posix()
    init = repo_dir / rel / "__init__.py"
    if init.is_file():
        return init.relative_to(repo_dir).as_posix()
    return None


def _js_missing_import_target_path(
    repo_dir: Path,
    importer_path: str,
    target: str,
) -> str | None:
    if not importer_path or not target or not target.startswith("."):
        return None
    importer = Path(importer_path.replace("\\", "/").strip().lstrip("./"))
    base = Path("/") / importer.parent / target
    rel_base = posixpath.normpath(base.as_posix()).lstrip("/")
    importer_suffix = importer.suffix.lower()
    suffixes = []
    if importer_suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        suffixes.append(importer_suffix)
    suffixes.extend([".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".mts", ".cts"])
    seen: set[str] = set()
    candidates: list[str] = []
    for suffix in suffixes:
        if suffix in seen:
            continue
        seen.add(suffix)
        candidates.append(f"{rel_base}{suffix}")
        candidates.append(f"{rel_base}/index{suffix}")
    for candidate in candidates:
        normalized = posixpath.normpath(candidate)
        if (repo_dir / normalized).is_file():
            return normalized
    return posixpath.normpath(f"{rel_base}{suffixes[0]}")


def _js_existing_import_target_path(
    repo_dir: Path,
    importer_path: str,
    target: str,
) -> str | None:
    candidate = _js_missing_import_target_path(repo_dir, importer_path, target)
    if not candidate:
        return None
    return candidate if (repo_dir / candidate).is_file() else None


def _artifact_findings_to_errors(
    repo_dir: Path,
    findings: list[ArtifactFinding],
) -> list[BuildError]:
    """Adapt artifact findings to the existing repatch error plumbing."""
    errors: list[BuildError] = []
    for finding in findings:
        error_file = finding.file
        if finding.code == "IMPORT_SYMBOL_MISSING" and finding.target:
            error_file = _python_module_to_source_path(repo_dir, finding.target) or error_file
        msg = finding.message
        if finding.symbol:
            msg = f"{msg} [symbol={finding.symbol}]"
        if finding.target:
            msg = f"{msg} [target={finding.target}]"
        errors.append(
            BuildError(
                file=error_file,
                line=None,
                message=f"{finding.code}: {msg}",
                raw=finding.raw or finding.message,
            )
        )
        if (
            finding.code == "IMPORT_SYMBOL_MISSING"
            and finding.target
            and finding.target.startswith(".")
        ):
            target_path = _js_existing_import_target_path(
                repo_dir, finding.file, finding.target
            )
            if target_path:
                errors.append(
                    BuildError(
                        file=target_path,
                        line=None,
                        message=(
                            f"IMPORT_SYMBOL_MISSING: importer {finding.file} expects "
                            f"`{finding.symbol}` to resolve from {finding.target}. "
                            f"Export or re-export that exact symbol from {target_path}."
                        ),
                        raw=finding.raw or finding.message,
                    )
                )
        if finding.code == "IMPORT_TARGET_MISSING":
            missing_js_target = _js_missing_import_target_path(
                repo_dir, finding.file, finding.target
            )
            if missing_js_target:
                symbol_hint = Path(finding.target).name.strip()
                hint = (
                    f" If this import path is intended, create/export the module at "
                    f"{missing_js_target}"
                    + (
                        f" and define/export `{symbol_hint}` there."
                        if symbol_hint else "."
                    )
                )
                errors.append(
                    BuildError(
                        file=missing_js_target,
                        line=None,
                        message=(
                            f"IMPORT_TARGET_MISSING: importer {finding.file} references "
                            f"missing relative module {finding.target}.{hint}"
                        ),
                        raw=finding.raw or finding.message,
                    )
                )
    return errors


def _prune_plan_to_error_files(
    plan: "PatchPlan | None",
    errors: list[BuildError],
) -> tuple["PatchPlan | None", int]:
    """Shrink a prior PatchPlan to only the edits implicated by build errors.

    On a repatch round the full prior plan (every FileEditPlan with all its
    preserved_findings) is otherwise re-inlined into the planner prompt via
    ``memory.format_for_prompt()``. For a broad change that is tens of
    thousands of JSON characters, which (a) bloats the prompt enough to push
    the SDK into the "success but empty structured_output" failure mode that
    crashed issue 010, and (b) is unlabeled in the prompt, so the planner is
    as likely to anchor on it as to revise it.

    "Keep only the error parts": retain just the edits whose ``filepath``
    matches a file named in this round's build errors (compile errors +
    deterministic findings). Files the prior plan got RIGHT (no error points
    at them) are dropped — the planner need not re-plan them, and the
    build_error_feedback section carries the actual "what to fix" signal.

    Returns (pruned_plan_or_None, dropped_count). When no edit matches (e.g.
    all errors are in test files the plan never touched), returns (None, n)
    so the caller clears the section entirely rather than keeping noise.
    """
    if plan is None or not plan.edits:
        return plan, 0

    def _norm(p: str) -> str:
        return p.replace("\\", "/").strip().lstrip("./")

    error_files = {_norm(e.file) for e in errors if e.file and e.file != "(build)"}
    if not error_files:
        # Un-attributable failure (synthetic "(build)" error): we cannot tell
        # which edits are implicated, so keep the plan as-is rather than guess.
        return plan, 0

    kept = [e for e in plan.edits if _norm(e.filepath) in error_files]
    dropped = len(plan.edits) - len(kept)
    if not kept:
        return None, dropped
    if dropped == 0:
        return plan, 0
    return PatchPlan(overview=plan.overview, edits=kept), dropped


def _augment_repair_plan_with_errors(
    plan: "PatchPlan | None",
    errors: list[BuildError],
    *,
    reason: str,
) -> "PatchPlan | None":
    """Ensure a direct repair plan carries the concrete verifier failures.

    Direct Stage2 repair bypasses the planner and invokes the patch-generator
    against a narrowed PatchPlan.  If the error text is only placed in working
    memory, a focused per-file prompt can still anchor on stale preserved
    findings.  Attach the blocking verifier messages to each implicated
    FileEditPlan as hard preserved findings, and synthesize a minimal edit plan
    when the original plan did not include the error file.
    """
    grouped: dict[str, list[str]] = {}
    symbols_by_path: dict[str, list[str]] = {}

    def _norm(path: str) -> str:
        return path.replace("\\", "/").strip().lstrip("./")

    def _should_attach_exact_symbol(err: BuildError) -> bool:
        message = err.message or ""
        if _GO_UNKNOWN_FIELD_RE.search(message):
            return True
        if _GO_UNDEFINED_QUALIFIED_RE.search(message):
            return True
        if _GO_CALL_QUALIFIED_RE.search(message):
            return True
        if _GO_TYPE_MEMBER_MISSING_RE.search(message):
            return True
        if "IMPORT_SYMBOL_MISSING" in message:
            return True
        if "IMPORT_TARGET_MISSING" in message:
            return True
        if _UNDEFINED_CONFIG_SYMBOL_RE.search(message):
            return True
        if "removed symbol still referenced" in message:
            return True
        if _GO_UNDEFINED_RE.search(message):
            return err.line is None
        return False

    def _attach_exact_symbol_to_error_path(path: str, err: BuildError) -> bool:
        message = err.message or ""
        if (
            _UNDEFINED_CONFIG_SYMBOL_RE.search(message)
            and not path.endswith(".go")
        ):
            return False
        if (
            (_GO_UNDEFINED_QUALIFIED_RE.search(message) or _GO_CALL_QUALIFIED_RE.search(message))
            and "cross-package compile repair context" not in message
        ):
            return False
        return True

    def _removed_symbol_compatibility_finding(path: str, message: str) -> str | None:
        m = _REMOVED_SYMBOL_NAME_RE.search(message)
        if not m:
            return None
        symbol = m.group(1)
        return (
            f"{reason}: {path}: MANDATORY compatibility repair for deleted "
            f"surface/member '{symbol}'. Restore a production code surface in "
            f"{path} spelled '{symbol}' with the same syntactic form that "
            "existing tests still compile against. If tests still construct or "
            "access it as a field/member, keep a compatibility field/member "
            "with that exact spelling on the owning production shape. If tests "
            "still import or call it, keep a thin alias, re-export, wrapper, "
            "or delegating shim unless the requirement explicitly removes the "
            "old API. For move/extract/refactor patches, keep the new canonical "
            "implementation, but also preserve the old compatibility surface. "
            "Do not edit tests, and do not merely update callers; both the "
            "canonical new path and the old compatibility path must resolve."
        )

    for err in errors:
        path = _norm(err.file or "")
        if not path or path == "(build)":
            continue
        loc = f"{path}:{err.line}" if err.line is not None else path
        bucket = grouped.setdefault(path, [])
        bucket.append(f"{reason}: {loc}: {err.message}")
        compat = _removed_symbol_compatibility_finding(path, err.message)
        if compat:
            bucket.append(compat)
        attach_exact_symbol = (
            _should_attach_exact_symbol(err)
            and _attach_exact_symbol_to_error_path(path, err)
        )
        if attach_exact_symbol:
            for symbol in _exact_symbols_from_build_error(err.message):
                bucket_symbols = symbols_by_path.setdefault(path, [])
                if symbol not in bucket_symbols:
                    bucket_symbols.append(symbol)

    if not grouped:
        return plan

    edits: list[FileEditPlan] = []
    seen_files: set[str] = set()
    merged_files: set[str] = set()
    if plan is not None:
        by_path: dict[str, list[FileEditPlan]] = {}
        order: list[str] = []
        for edit in plan.edits:
            path = _norm(edit.filepath)
            seen_files.add(path)
            if path not in by_path:
                by_path[path] = []
                order.append(path)
            by_path[path].append(edit)

        for path in order:
            existing = by_path[path]
            additions = grouped.get(path, [])
            if not additions:
                edits.extend(existing)
                continue
            merged_files.add(path)
            findings: list[str] = []
            co_edits: list[str] = []
            expected_symbols: list[str] = []
            requirement_ids: list[str] = []
            rationales: list[str] = []
            target_functions: list[str] = []
            for edit in existing:
                for target in edit.target_functions:
                    if target not in target_functions:
                        target_functions.append(target)
                for item in edit.preserved_findings:
                    if item not in findings:
                        findings.append(item)
                for dep in edit.co_edit_dependencies:
                    if dep not in co_edits:
                        co_edits.append(dep)
                for symbol in edit.expected_symbols:
                    if symbol not in expected_symbols:
                        expected_symbols.append(symbol)
                for req_id in edit.required_by_requirement_ids:
                    if req_id not in requirement_ids:
                        requirement_ids.append(req_id)
                rationale = (edit.change_rationale or "").strip()
                if rationale and rationale not in rationales:
                    rationales.append(rationale)
            for item in additions:
                if item not in findings:
                    findings.append(item)
            for symbol in symbols_by_path.get(path, []):
                if symbol not in expected_symbols:
                    expected_symbols.append(symbol)
            keep_target_scope = bool(target_functions)
            edits.append(
                FileEditPlan(
                    filepath=existing[0].filepath,
                    target_functions=target_functions if keep_target_scope else [],
                    change_rationale=_compose_change_rationale(
                        header=(
                            f"Direct Stage2 repair for {reason}; resolve the "
                            "blocking verifier findings across this file in one "
                            + (
                                "coherent targeted repair while preserving the "
                                "planner's original localization."
                                if keep_target_scope else
                                "coherent file-wide pass while preserving the "
                                "intended requirement-level behavior."
                            )
                        ),
                        rationales=rationales,
                    ),
                    preserved_findings=findings,
                    co_edit_dependencies=co_edits,
                    reference_only=all(edit.reference_only for edit in existing),
                    expected_diff_required=any(
                        edit.expected_diff_required for edit in existing
                    ),
                    creates_new_file=any(edit.creates_new_file for edit in existing),
                    expected_symbols=expected_symbols,
                    required_by_requirement_ids=requirement_ids,
                )
            )

    for path, messages in grouped.items():
        if path in seen_files or path in merged_files:
            continue
        creates_new_file = any(
            "create/export the module at" in message for message in messages
        )
        edits.append(
            FileEditPlan(
                filepath=path,
                target_functions=[],
                change_rationale=(
                    f"Direct Stage2 repair for {reason}; fix the blocking "
                    "verification findings in this production file."
                ),
                preserved_findings=messages,
                co_edit_dependencies=[],
                creates_new_file=creates_new_file,
                expected_symbols=list(symbols_by_path.get(path, [])),
            )
        )

    if not edits:
        return plan

    overview = (
        plan.overview if plan is not None else
        f"Direct Stage2 repair for {reason} verifier findings."
    )
    repaired = PatchPlan(overview=overview, edits=edits)
    # Compile errors in one production file form one consistency problem.
    # Re-splitting by finding count caused duplicate helper definitions and
    # repeated model calls in later repair rounds.
    return repaired


def _reroute_test_compile_errors_to_production_files(
    plan: "PatchPlan | None",
    errors: list[BuildError],
) -> list[BuildError]:
    """Map test-file compile errors onto nearby production edit targets.

    Stage2 compile repair must not spend budget editing evaluator-owned tests:
    those edits are reverted immediately before verification. When a package
    test fails to compile against patched production code, route the failure to
    same-directory production files already present in the narrowed repair
    context so the generator repairs the compatibility surface instead.
    """

    def _norm(path: str) -> str:
        return path.replace("\\", "/").strip().lstrip("./")

    planned_by_dir: dict[str, list[str]] = {}
    if plan is not None:
        for edit in plan.edits:
            path = _norm(edit.filepath)
            if not path or is_test_file(path):
                continue
            dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
            planned_by_dir.setdefault(dir_path, []).append(path)

    routed: list[BuildError] = []
    seen: set[tuple[str, int | None, str]] = set()
    for err in errors:
        path = _norm(err.file or "")
        if not path or not is_test_file(path):
            key = (err.file, err.line, err.message)
            if key not in seen:
                seen.add(key)
                routed.append(err)
            continue
        dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
        targets = planned_by_dir.get(dir_path) or []
        if not targets:
            key = (err.file, err.line, err.message)
            if key not in seen:
                seen.add(key)
                routed.append(err)
            continue
        origin = f"{path}:{err.line}" if err.line is not None else path
        for target in targets:
            message = (
                f"test compile compatibility failure from {origin}: "
                f"{err.message}"
            )
            key = (target, None, message)
            if key in seen:
                continue
            seen.add(key)
            routed.append(BuildError(file=target, line=None, message=message, raw=err.raw))
    return routed


def _expand_go_same_package_repair_context(
    plan: "PatchPlan | None",
    errors: list[BuildError],
) -> list[BuildError]:
    """Broaden Go compile repair to sibling production files in the same package.

    Undefined-symbol and similar compatibility errors often surface at the
    callsite file while the real fix belongs in a sibling file in the same Go
    package that defines the production surface. Keep the original error, then
    mirror it onto other planned production files in that directory so repair
    can touch both callsite and definition files in one round.
    """

    def _norm(path: str) -> str:
        return path.replace("\\", "/").strip().lstrip("./")

    planned_by_dir: dict[str, list[str]] = {}
    if plan is not None:
        for edit in plan.edits:
            path = _norm(edit.filepath)
            if not path or is_test_file(path) or not path.endswith(".go"):
                continue
            dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
            planned_by_dir.setdefault(dir_path, []).append(path)

    expanded: list[BuildError] = list(errors)
    seen = {(err.file, err.line, err.message) for err in errors}
    for err in errors:
        path = _norm(err.file or "")
        if not path.endswith(".go") or is_test_file(path):
            continue
        if not _GO_SIG_ERROR_RE.search(err.message or ""):
            continue
        dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
        targets = planned_by_dir.get(dir_path) or []
        if len(targets) < 2:
            continue
        origin = f"{path}:{err.line}" if err.line is not None else path
        for target in targets:
            if target == path:
                continue
            message = f"same-package compile repair context from {origin}: {err.message}"
            key = (target, None, message)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(BuildError(file=target, line=None, message=message, raw=err.raw))
    return expanded


def _expand_go_cross_package_owner_context(
    repo_dir: Path,
    plan: "PatchPlan | None",
    errors: list[BuildError],
) -> list[BuildError]:
    """Mirror Go compile errors onto imported local-package owner files.

    Same-package context is insufficient when a caller drifts from an imported
    package's real API. If the narrowed repair plan already contains likely
    owner files in that imported package, surface the same verifier failure on
    those files too so direct repair can revise caller and provider together.
    """

    def _norm(path: str) -> str:
        return path.replace("\\", "/").strip().lstrip("./")

    planned_by_dir: dict[str, list[str]] = {}
    if plan is not None:
        for edit in plan.edits:
            path = _norm(edit.filepath)
            if not path or is_test_file(path) or not path.endswith(".go"):
                continue
            dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
            planned_by_dir.setdefault(dir_path, []).append(path)

    module = _go_module_path(repo_dir)
    if not module:
        return errors

    expanded: list[BuildError] = list(errors)
    seen = {(err.file, err.line, err.message) for err in errors}
    file_cache: dict[str, str] = {}

    for err in errors:
        caller = _norm(err.file or "")
        if not caller.endswith(".go") or is_test_file(caller):
            continue
        if caller not in file_cache:
            try:
                file_cache[caller] = (repo_dir / caller).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                file_cache[caller] = ""
        aliases = _go_import_aliases(file_cache[caller])
        if not aliases:
            continue

        requests: list[tuple[str, str]] = []
        requests.extend(_GO_UNDEFINED_QUALIFIED_RE.findall(err.message or ""))
        requests.extend(_GO_CALL_QUALIFIED_RE.findall(err.message or ""))
        for alias, type_name, member in _GO_TYPE_MEMBER_MISSING_RE.findall(err.message or ""):
            requests.append((alias, type_name))
            requests.append((alias, member))

        origin = f"{caller}:{err.line}" if err.line is not None else caller
        routed_targets: set[str] = set()
        caller_dir = caller.rsplit("/", 1)[0] if "/" in caller else ""
        for alias, symbol in requests:
            import_path = aliases.get(alias)
            if not import_path or not import_path.startswith(module + "/"):
                continue
            package_dir = _go_local_import_path_to_dir(repo_dir, import_path)
            if package_dir is None:
                continue
            try:
                rel_dir = package_dir.relative_to(repo_dir).as_posix()
            except ValueError:
                continue
            if rel_dir == caller_dir:
                continue

            targets = _go_package_definition_files(repo_dir, package_dir, symbol)
            if not targets:
                targets = list(planned_by_dir.get(rel_dir, []))
            if not targets:
                continue

            for target in targets:
                if target in routed_targets:
                    continue
                routed_targets.add(target)
                message = (
                    f"cross-package compile repair context from {origin} via "
                    f"import {alias}={import_path}: {err.message}"
                )
                key = (target, None, message)
                if key in seen:
                    continue
                seen.add(key)
                expanded.append(
                    BuildError(file=target, line=None, message=message, raw=err.raw)
                )
    return expanded


def _expand_config_symbol_owner_context(
    repo_dir: Path,
    plan: "PatchPlan | None",
    errors: list[BuildError],
) -> list[BuildError]:
    """Mirror undefined config-symbol findings onto likely production owners.

    Static closure can report the drift on a schema/config artifact even though
    the actual repair belongs in production config code. Expand those findings
    onto likely owner-side Go files so direct Stage2 repair can touch the
    definition site instead of anchoring only on the artifact file.
    """

    def _norm(path: str) -> str:
        return path.replace("\\", "/").strip().lstrip("./")

    planned_go_files: list[str] = []
    if plan is not None:
        for edit in plan.edits:
            path = _norm(edit.filepath)
            if not path or is_test_file(path) or not path.endswith(".go"):
                continue
            planned_go_files.append(path)

    repo_go_files: list[str] = []
    for path in sorted(repo_dir.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            rel = path.relative_to(repo_dir).as_posix()
        except ValueError:
            continue
        repo_go_files.append(rel)

    def _config_scored(candidates: list[str]) -> list[str]:
        scored: list[tuple[int, str]] = []
        for candidate in candidates:
            score = 0
            lowered = candidate.lower()
            if "/config/" in lowered or lowered.startswith("config/"):
                score += 4
            if lowered.endswith("/config.go") or lowered.endswith("config.go"):
                score += 2
            scored.append((score, candidate))
        return [path for _, path in sorted(scored, key=lambda item: (-item[0], item[1]))]

    expanded: list[BuildError] = list(errors)
    seen = {(err.file, err.line, err.message) for err in errors}
    for err in errors:
        match = _UNDEFINED_CONFIG_SYMBOL_RE.search(err.message or "")
        source_path = _norm(err.file or "")
        if not match or not source_path or source_path == "(build)":
            continue
        symbol = match.group(1)
        owner_targets: list[str] = []
        for candidate in _config_scored(planned_go_files):
            if _go_file_mentions_symbol(repo_dir, candidate, symbol) and candidate not in owner_targets:
                owner_targets.append(candidate)
        if not owner_targets:
            for candidate in _config_scored(repo_go_files):
                if _go_file_mentions_symbol(repo_dir, candidate, symbol) and candidate not in owner_targets:
                    owner_targets.append(candidate)
                if len(owner_targets) >= 3:
                    break
        if not owner_targets:
            fallback_pool = _config_scored(planned_go_files) or _config_scored(repo_go_files)
            owner_targets.extend(fallback_pool[:2])
        for target in owner_targets:
            if target == source_path:
                continue
            message = (
                f"config-symbol repair context from {source_path} for symbol "
                f"{symbol}: {err.message}"
            )
            key = (target, None, message)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(
                BuildError(file=target, line=None, message=message, raw=err.raw)
            )
    return expanded


_RATIONALE_THEME_MAX_ITEMS = 8
_RATIONALE_THEME_MAX_CHARS = 2400


def _normalize_rationale_theme(text: str) -> str:
    """Strip nested boilerplate so repair themes do not grow recursively."""
    normalized = (text or "").strip()
    if not normalized:
        return ""
    if "\nOriginal planner themes:\n" in normalized:
        normalized = normalized.split("\nOriginal planner themes:\n", 1)[0].strip()
    normalized = " ".join(normalized.split())
    return normalized


def _compact_rationale_themes(rationales: list[str]) -> list[str]:
    """Deduplicate and bound rationale themes for repair/merge prompts."""
    kept: list[str] = []
    total_chars = 0
    for raw in rationales:
        theme = _normalize_rationale_theme(raw)
        if not theme or theme in kept:
            continue
        if len(theme) > 280:
            theme = theme[:277].rstrip() + "..."
        projected = total_chars + len(theme)
        if kept and (
            len(kept) >= _RATIONALE_THEME_MAX_ITEMS
            or projected > _RATIONALE_THEME_MAX_CHARS
        ):
            break
        kept.append(theme)
        total_chars = projected
    return kept


def _compose_change_rationale(
    *,
    header: str,
    rationales: list[str] | None = None,
) -> str:
    """Compose a bounded rationale string from a header and prior themes."""
    compact = _compact_rationale_themes(list(rationales or []))
    if not compact:
        return header
    return header + "\nOriginal planner themes:\n- " + "\n- ".join(compact)


def _merge_patch_plans(
    base: "PatchPlan | None",
    extra: "PatchPlan | None",
) -> "PatchPlan | None":
    """Union two plans so final artifact verification keeps all obligations."""
    if base is None:
        return extra
    if extra is None:
        return base

    def _merge_lists(left: list[str], right: list[str]) -> list[str]:
        merged = list(left)
        for item in right:
            if item not in merged:
                merged.append(item)
        return merged

    merged_by_path: dict[str, FileEditPlan] = {}
    order: list[str] = []
    for edit in [*base.edits, *extra.edits]:
        path = edit.filepath
        if path not in merged_by_path:
            merged_by_path[path] = edit.model_copy(deep=True)
            order.append(path)
            continue
        current = merged_by_path[path]
        merged_by_path[path] = FileEditPlan(
            filepath=current.filepath,
            target_functions=_merge_lists(current.target_functions, edit.target_functions),
            change_rationale=_compose_change_rationale(
                header=_normalize_rationale_theme(current.change_rationale)
                or _normalize_rationale_theme(edit.change_rationale)
                or "Merged repair themes for this file.",
                rationales=[current.change_rationale, edit.change_rationale],
            ),
            preserved_findings=_merge_lists(current.preserved_findings, edit.preserved_findings),
            co_edit_dependencies=_merge_lists(current.co_edit_dependencies, edit.co_edit_dependencies),
            reference_only=current.reference_only and edit.reference_only,
            expected_diff_required=current.expected_diff_required or edit.expected_diff_required,
            creates_new_file=current.creates_new_file or edit.creates_new_file,
            expected_symbols=_merge_lists(current.expected_symbols, edit.expected_symbols),
            required_by_requirement_ids=_merge_lists(
                current.required_by_requirement_ids,
                edit.required_by_requirement_ids,
            ),
        )

    merged_plan = PatchPlan(
        overview=base.overview if base.overview == extra.overview else base.overview,
        edits=[merged_by_path[path] for path in order],
    )
    _split_heavy_edits(merged_plan)
    return merged_plan


def _render_heuristic_feedback(
    contract_drift: list[BuildError],
    parallel_impl: list[BuildError],
    removed_sym_refs: list[BuildError],
    go_unexport: list[BuildError],
    config_shape: list[BuildError],
    python_noniterable: list[BuildError],
    python_helper_api: list[BuildError],
    python_config_subscript: list[BuildError],
    python_moved_class_dunder: list[BuildError],
    active: list[BuildError],
    residues: list[BuildError] | None = None,
    config_sym_errors: list[BuildError] | None = None,
) -> str:
    """Render only the *active* (non-downgraded) findings, grouped by gate."""
    active_sigs = {_gate_signature(e) for e in active}

    def _keep(errs: list[BuildError]) -> list[BuildError]:
        return [e for e in errs if _gate_signature(e) in active_sigs]

    parts: list[str] = []
    if residues:
        kept = _keep(residues)
        if kept:
            parts.append(render_residue_for_feedback(kept))
    if config_sym_errors:
        kept = _keep(config_sym_errors)
        if kept:
            parts.append(render_undefined_config_symbol_for_feedback(kept))
    for errs, renderer in (
        (contract_drift, render_contract_drift_for_feedback),
        (parallel_impl, render_parallel_impl_for_feedback),
        (removed_sym_refs, render_removed_symbol_test_refs_for_feedback),
        (go_unexport, render_go_unexport_for_feedback),
        (config_shape, render_config_entry_shape_for_feedback),
        (python_noniterable, render_python_noniterable_class_loop_for_feedback),
        (python_helper_api, render_python_helper_api_usage_for_feedback),
        (python_config_subscript, render_python_config_subscript_fallback_for_feedback),
        (python_moved_class_dunder, render_python_moved_class_dunder_methods_for_feedback),
    ):
        kept = _keep(errs)
        if kept:
            parts.append(renderer(kept))
    return "\n\n".join(p for p in parts if p)


def _grounded_requirement_ids(evidence: EvidenceCards | None) -> list[str]:
    """Return req IDs that a degraded (best-effort) patch could safely act on.

    A requirement is "grounded" when it has an actionable verdict
    (AS_IS_VIOLATED / TO_BE_MISSING / TO_BE_PARTIAL) AND at least one
    ``evidence_location`` — i.e. a concrete file:line the patch-planner can
    target.  Compliant and still-UNCHECKED requirements contribute nothing to
    a patch and are excluded.

    This is the discriminator between the two EVIDENCE_INCOMPLETE failure
    shapes observed in eval:

    - issue 004 (qutebrowser): every requirement carried zero verified
      evidence_locations — deep-search kept claiming Reads it never performed,
      so token-traceability self-correction wiped the locations each round.
      Nothing is grounded → a degraded patch would be pure guesswork → stay
      EVIDENCE_INCOMPLETE (correct to skip).

    - issue 009 (teleport): req-006 was fully verified (20 locations, the
      whole ForwarderConfig field-rename set), and req-002/req-004 also had
      real locations.  Plenty is grounded → a patch covering just the verified
      renames is far better than a guaranteed-zero EVIDENCE_INCOMPLETE.
    """
    if evidence is None:
        return []
    actionable = {"AS_IS_VIOLATED", "TO_BE_MISSING", "TO_BE_PARTIAL"}
    grounded: list[str] = []
    for req in evidence.requirements:
        if req.verdict in actionable and req.evidence_locations:
            grounded.append(req.id)
    return grounded


def _build_deep_search_todo(
    target: RequirementItem,
    rework_context: str = "",
    force_read_directive: str = "",
) -> str:
    """Build a scoped TODO for one RequirementItem.

    If *rework_context* is non-empty, this requirement was previously given
    a verdict that the closure-checker flagged as inconsistent.  The context
    (closure rationale + conflicting locations + other implicated requirement
    IDs) is appended so the model can deliberately resolve the contradiction
    rather than repeating the prior verdict.

    If *force_read_directive* is non-empty, a prior round for this requirement
    produced a verdict without actually calling Read on any source file
    (retrieved_code did not grow).  The directive is injected at the TOP of the
    TODO — the most salient position — to break the "claim a Read that never
    happened → token-traceability strips the locations → loop" failure mode
    observed on issues 004/011.
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
    if force_read_directive:
        base = force_read_directive + "\n\n" + base
    if rework_context:
        base += (
            "\n\n── REWORK CONTEXT ─────────────────────────────\n"
            "The previous verdict/localization for this requirement was "
            "flagged by the closure-checker as semantically insufficient or "
            "inconsistent. Re-read the cited file regions carefully and "
            "produce a verdict plus concrete writable evidence fields that "
            "resolve the closure feedback. Do not simply repeat the prior "
            "verdict or the same localization path.\n\n"
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
    # The persisted AS-IS observation fields are exactly the deep-search-owned
    # fields (single source of truth in ingestion_tools). Reusing the tuple here
    # guarantees a new field added to update_localization is also forwarded from
    # the DeepSearchReport — the gap that previously stranded consistency_anchors.
    for attr in DEEP_SEARCH_OWNED_FIELDS:
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


def _reconcile_final_patch_outcome(
    patch_outcome: str | None,
    closure_approved: bool,
    *,
    artifact_ok: bool,
    artifact_empty_patch: bool,
) -> tuple[str | None, bool]:
    """Apply final artifact-verification downgrades without erasing infra exits."""
    if artifact_ok:
        return patch_outcome, closure_approved
    closure_approved = False
    if artifact_empty_patch:
        if patch_outcome != "MODEL_INFRA_FAILURE":
            patch_outcome = "NO_EFFECT_PATCH"
        return patch_outcome, closure_approved
    if patch_outcome in (None, "PATCH_SUCCESS", "PATCH_FAILED"):
        patch_outcome = "PATCH_INCOMPLETE"
    return patch_outcome, closure_approved


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
    except ModelInfrastructureError:
        raise
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
    except ModelInfrastructureError:
        raise
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
# Checkpoint helpers
# ══════════════════════════════════════════════════════════════════════════

_CHECKPOINT_FILENAME = "checkpoint.json"


def _model_runtime_metadata() -> dict[str, str]:
    backend = (
        os.environ.get("MODEL_BACKEND")
        or os.environ.get("LLM_BACKEND")
        or "anthropic"
    ).strip().lower()
    if backend in {"claude", "anthropic"}:
        backend = "anthropic"
        model = os.environ.get("ANTHROPIC_MODEL", "")
        api_surface = "claude_agent_sdk"
    elif backend in {"openai", "codex", "codex-pro"}:
        backend = "openai"
        model = (
            os.environ.get("OPENAI_MODEL")
            or os.environ.get("CODEX_PRO_MODEL")
            or os.environ.get("ANTHROPIC_MODEL", "")
        )
        api_surface = os.environ.get("OPENAI_API_SURFACE", "chat_completions")
    else:
        model = ""
        api_surface = ""
    return {
        "model_backend": backend,
        "model": model,
        "api_surface": api_surface,
    }


def _save_checkpoint(
    output_dir: Path,
    state: "PipelineState",
    memory: "SharedWorkingMemory",
    counters: dict,
    ltm_query: str = "",
    custom_route_query: str = "",
    aggregate_patch_plan: "PatchPlan | None" = None,
) -> None:
    payload = {
        "version": "1",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "runtime": _model_runtime_metadata(),
        "pipeline_state": state.value,
        "ltm_query": ltm_query,
        "custom_route_query": custom_route_query,
        "budget_counters": counters,
        "working_memory": json.loads(memory.model_dump_json()),
        "aggregate_patch_plan": (
            None
            if aggregate_patch_plan is None
            else json.loads(aggregate_patch_plan.model_dump_json())
        ),
    }
    (output_dir / _CHECKPOINT_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _load_checkpoint(output_dir: Path) -> dict | None:
    p = output_dir / _CHECKPOINT_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _delete_checkpoint(output_dir: Path) -> None:
    p = output_dir / _CHECKPOINT_FILENAME
    if p.exists():
        p.unlink()


def _pack_counters(
    budget: "DeepSearchBudget",
    rework_rounds_used: int,
    patch_verify_rounds_used: int,
    plan_coverage_rounds_used: int,
    per_req_unchecked_count: dict,
    closure_failure_streak: int,
    rework_rounds_by_req: dict[str, int] | None = None,
) -> dict:
    return {
        "deep_search_iterations_done": budget.iteration,
        "rework_rounds_used": rework_rounds_used,
        "rework_rounds_by_req": dict(rework_rounds_by_req or {}),
        "patch_verify_rounds_used": patch_verify_rounds_used,
        "plan_coverage_rounds_used": plan_coverage_rounds_used,
        "per_req_unchecked_count": per_req_unchecked_count,
        "closure_failure_streak": closure_failure_streak,
    }


async def run_pipeline_from_checkpoint(
    issue_id: str,
    repo_dir: "str | Path",
    output_dir: "str | Path",
    checkpoint: dict,
    *,
    stop_after_closure: bool = False,
    retry_failed_closure: bool = False,
) -> Path:
    """Resume a pipeline run from a previously saved checkpoint.

    Restores the full SharedWorkingMemory (evidence cards, LTM blocks, etc.)
    and the budget counters, then re-enters the state machine at the
    checkpointed PipelineState.  The caller (main.py) is responsible for
    resetting the repo to base_commit before invoking this function, exactly
    as it does before a fresh run.
    """
    from src.models.memory import SharedWorkingMemory
    from src.tools.ingestion_tools import restore_working_memory

    repo_dir = Path(repo_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    memory = SharedWorkingMemory.model_validate(checkpoint["working_memory"])
    restore_working_memory(memory)
    set_repo_root(repo_dir)

    evidence_path = (output_dir / "evidence.json").resolve()
    set_evidence_json_path(evidence_path)
    # Re-sync evidence.json on disk so grounding helpers read the restored state.
    evidence_path.write_text(
        memory.evidence_cards.model_dump_json(indent=2), encoding="utf-8"
    )

    initial_state = PipelineState(checkpoint["pipeline_state"])
    counters = checkpoint.get("budget_counters") or {}
    if retry_failed_closure:
        if not stop_after_closure:
            raise ValueError(
                "closure-only retry requires stop_after_closure=True"
            )
        legacy_exhausted_handoff = (
            initial_state == PipelineState.UNDER_SPECIFIED
            and int(counters.get("deep_search_iterations_done", 0)) >= 30
            and int(counters.get("rework_rounds_used", 0)) >= 3
        )
        if (
            initial_state != PipelineState.CLOSURE_FORCED_FAIL
            and not legacy_exhausted_handoff
        ):
            raise ValueError(
                "closure-only retry requires a ClosureForcedFail checkpoint "
                "or a legacy exhausted 30/30, 3/3 analysis checkpoint"
            )
        unchecked = [
            req.id
            for req in memory.evidence_cards.requirements
            if req.verdict == "UNCHECKED"
        ]
        if unchecked:
            raise ValueError(
                "closure-only retry refuses unchecked requirements: "
                + ", ".join(unchecked)
            )
        memory.record_action(
            phase="closure-check",
            subagent="closure-checker",
            outcome="closure_only_retry:budget_preserved",
        )
        initial_state = PipelineState.EVIDENCE_REFINING
        print(
            "[resume] Explicit closure-only retry: preserving evidence, action "
            "history and all budget counters; deep-search remains exhausted.",
            flush=True,
        )
    if stop_after_closure and initial_state == PipelineState.CLOSED:
        print("[resume] Analysis checkpoint is already CLOSED.", flush=True)
        return evidence_path
    print(
        f"[resume] Restoring pipeline at state={initial_state.value} "
        f"(checkpoint saved {checkpoint.get('saved_at', '?')})",
        flush=True,
    )

    return await _run_state_machine_managed(
        issue_id=issue_id,
        repo_dir=repo_dir,
        output_dir=output_dir,
        evidence_path=evidence_path,
        memory=memory,
        initial_state=initial_state,
        ltm_query=checkpoint.get("ltm_query", ""),
        custom_route_query=checkpoint.get("custom_route_query", ""),
        initial_counters=checkpoint.get("budget_counters"),
        initial_aggregate_patch_plan=(
            PatchPlan.model_validate(checkpoint["aggregate_patch_plan"])
            if checkpoint.get("aggregate_patch_plan")
            else None
        ),
        stop_after_closure=stop_after_closure,
        closure_only_retry=retry_failed_closure,
    )


# ══════════════════════════════════════════════════════════════════════════
# Code-driven pipeline
# ══════════════════════════════════════════════════════════════════════════

async def run_pipeline(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
    problem_statement: str = "",
    *,
    stop_after_closure: bool = False,
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

    return await _run_state_machine_managed(
        issue_id=issue_id,
        repo_dir=repo_dir,
        output_dir=output_dir,
        evidence_path=evidence_path,
        memory=memory,
        initial_state=PipelineState.UNDER_SPECIFIED,
        ltm_query=ltm_query,
        custom_route_query=custom_route_query,
        stop_after_closure=stop_after_closure,
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

    return await _run_state_machine_managed(
        issue_id=issue_id,
        repo_dir=repo_dir,
        output_dir=output_dir,
        evidence_path=evidence_path,
        memory=memory,
        initial_state=PipelineState.EVIDENCE_REFINING,
        ltm_query=(problem_statement or memory.issue_context).strip(),
    )


async def _run_state_machine_managed(**kwargs) -> Path:
    try:
        return await _run_state_machine(**kwargs)
    finally:
        await close_structured_clients()


async def _run_state_machine(
    issue_id: str,
    repo_dir: Path,
    output_dir: Path,
    evidence_path: Path,
    memory,
    initial_state: PipelineState,
    ltm_query: str = "",
    custom_route_query: str = "",
    initial_counters: dict | None = None,
    initial_aggregate_patch_plan: "PatchPlan | None" = None,
    stop_after_closure: bool = False,
    closure_only_retry: bool = False,
) -> Path:
    """Core state-machine loop shared by run_pipeline and run_pipeline_from_evidence."""
    state = initial_state
    _c = initial_counters or {}
    initial_evidence = get_submitted_evidence()
    requirement_count = (
        len(initial_evidence.requirements) if initial_evidence is not None else 0
    )
    deep_search_limit = _deep_search_iteration_limit(requirement_count)
    budget = DeepSearchBudget(max_iterations=deep_search_limit)
    if deep_search_limit > 30:
        print(
            "[budget] expanded deep-search budget for "
            f"{requirement_count} requirements: {deep_search_limit}",
            flush=True,
        )
    budget.iteration = _restore_deep_search_iteration(
        _c.get("deep_search_iterations_done", 0), memory.action_history,
    )
    # Hard wall-clock cap per deep-search round. The SDK already has per-query
    # max_turns / max_budget_usd, but a wedged subprocess (e.g. an OOM-killed
    # child the parent never reaps) can hang the await forever. wait_for turns
    # that into a recorded iteration failure so the budget keeps advancing.
    deep_search_timeout_s = 1200.0
    last_verdict: ClosureVerdict | None = None
    patch_outcome: str | None = None
    forced_closure_done: bool = False
    # Degraded (best-effort) patch mode (improvement 3): when the pipeline would
    # otherwise terminate as EVIDENCE_INCOMPLETE (closure never approved, budget
    # spent / rework exhausted), but enough requirements are *grounded* (have an
    # actionable verdict AND ≥1 evidence_location), we route to patch planning
    # anyway and emit a PARTIAL_PATCH instead of a guaranteed-zero
    # EVIDENCE_INCOMPLETE.  Gated on grounded-evidence COUNT, not closure
    # approval, so issue-004-style hollow evidence (zero verified locations
    # anywhere) still correctly skips patching rather than guessing.
    degraded_patch_mode: bool = False
    _DEGRADED_MIN_GROUNDED = 1
    closure_retry_limit = 2
    closure_failure_streak = _c.get("closure_failure_streak", 0)
    max_closure_failure_streak = 3
    # Rework budget: each EVIDENCE_MISSING with parseable req IDs re-opens
    # those requirements (verdict=UNCHECKED, scope cleared) and feeds the
    # closure-checker's rationale into deep-search as rework context.  The
    # total counter is retained for metrics.  Eligibility is enforced per
    # requirement so one stubborn req cannot consume every precise rework slot
    # and prevent a newly identified blocking req from being investigated.
    rework_rounds_used = _c.get("rework_rounds_used", 0)
    rework_rounds_by_req = {
        str(k): int(v)
        for k, v in (_c.get("rework_rounds_by_req", {}) or {}).items()
    }
    per_req_rework_rounds_max = 3

    # Per-requirement retry counter: prevents infinite loops where the same
    # requirement keeps returning UNCHECKED and starving other requirements.
    per_req_unchecked_count: dict[str, int] = _c.get("per_req_unchecked_count", {})
    per_req_unchecked_max = 3

    # Post-patch compile verification permits a small number of direct
    # patch-generator repairs for new file/line-attributable errors. It never
    # re-enters planning. Static gates still get at most one repair attempt
    # because repeated static warnings usually add no new information.
    patch_verify_rounds_used = _c.get("patch_verify_rounds_used", 0)
    patch_verify_rounds_max = 3
    artifact_repair_rounds_used = 0
    artifact_repair_rounds_max = 1
    # True once a build command demonstrably ran this session — a later
    # "unverifiable" verdict is then likely transient and retried once.
    toolchain_seen_working = False
    # Phase-27 heuristic-gate false-positive fuse: signatures of gate findings
    # already fed back to the planner. If a finding survives a repatch round
    # unchanged (the planner saw it and deliberately kept the edit), it is
    # downgraded to a warning instead of consuming budget again.
    fed_back_gate_signatures: set[str] = set()
    # Spec-priority firewall: how many times we re-planned because the plan
    # failed to cover an AS_IS_VIOLATED requirement's cited file. Capped to
    # avoid a loop when the planner genuinely cannot/won't cover it.
    plan_coverage_rounds_used = _c.get("plan_coverage_rounds_used", 0)
    plan_coverage_rounds_max = 1
    # LTM/custom-rule retrieval for planning is expensive; run it once on the
    # first CLOSED entry and reuse the cached blocks on repatch rounds.
    planner_ltm_loaded = False
    # Every filepath any plan round intended to touch — used so the final
    # git-diff promotes ALL created files (a repatch overwrites memory.patch_plan
    # with the fixup plan, which would otherwise drop first-round new files).
    all_planned_files: set[str] = set()
    aggregate_patch_plan = initial_aggregate_patch_plan
    if aggregate_patch_plan is not None:
        all_planned_files.update(edit.filepath for edit in aggregate_patch_plan.edits)
    # Initial/final compile records, written to compile_check.json.
    build_verify_log: list[dict[str, Any]] = []
    # Per-round patch artifact verification records, written to
    # artifact_verification.json.
    artifact_verify_log: list[dict[str, Any]] = []
    # Consecutive deep-search failure counter per requirement id.
    # Prevents infinite retry when a requirement keeps failing before verdict is set.
    _ds_fail_counts: dict[str, int] = {}
    _DS_FAIL_MAX = 3

    # Per-requirement stall detection (improvement 2): deep-search on issues
    # 004/009 burned the entire 30-iteration budget re-investigating the same
    # requirements without ever adding a verified evidence_location — each round
    # the agent claimed a Read it never performed, token-traceability wiped the
    # locations, the verdict reverted, and the loop repeated.  We detect this by
    # snapshotting a per-requirement progress signature (verdict + #locations +
    # #cached snippets) each time it is investigated; if the signature does not
    # improve for _DS_STALL_MAX consecutive visits, the requirement is frozen
    # (left at its current verdict, removed from the rework pool) so the saved
    # budget can flow to patch planning instead of an unwinnable spin.
    _ds_stall_signatures: dict[str, tuple] = {}
    _ds_stall_counts: dict[str, int] = {}
    _ds_frozen_reqs: set[str] = set()
    _DS_STALL_MAX = 3
    # Restore frozen requirements from action_history (survives container restarts).
    for _evt in memory.action_history:
        if _evt.outcome.startswith("frozen_stalled:") and _evt.requirement_id:
            _ds_frozen_reqs.add(_evt.requirement_id)
    # Consecutive "hollow" rounds per requirement: a round is hollow when it
    # lands ZERO verified evidence_locations (the 004/011 signature — claimed
    # verdict, attribution/traceability strips the cites to empty).  After one
    # hollow round we arm a forced-Read directive on the NEXT todo for that
    # requirement, instructing the agent to actually open files before
    # asserting a verdict.  retrieved_code can't be used to detect this (it is
    # dead — wired to no agent), so the location count is the only signal.
    _ds_hollow_counts: dict[str, int] = {}
    _DS_HOLLOW_FORCE_READ = 1

    _terminal_states = (
        PipelineState.PATCH_SUCCESS,
        PipelineState.PATCH_FAILED,
        PipelineState.CLOSURE_FORCED_FAIL,
    )

    def _route_forced_fail() -> PipelineState:
        """Decide where a forced-fail exit goes.

        Returns CLOSED (degraded patch) when enough requirements are grounded,
        else CLOSURE_FORCED_FAIL (skip patching). Sets degraded_patch_mode and
        patch_outcome as a side effect. Centralizes improvement 3 so every
        forced-fail site shares one policy.
        """
        nonlocal degraded_patch_mode, patch_outcome
        grounded = _grounded_requirement_ids(get_submitted_evidence())
        if stop_after_closure:
            patch_outcome = "EVIDENCE_INCOMPLETE"
            print(
                "[orchestrator] analysis-only closure failed; refusing degraded "
                "patch planning and leaving no resumable CLOSED checkpoint.",
                flush=True,
            )
            memory.record_action(
                phase="closure-check", subagent="closure-checker",
                outcome="analysis_closure_failed:no_degraded_patch",
            )
            return PipelineState.CLOSURE_FORCED_FAIL
        if len(grounded) >= _DEGRADED_MIN_GROUNDED:
            degraded_patch_mode = True
            print(
                "[orchestrator] closure not approved, but "
                f"{len(grounded)} grounded requirement(s) {grounded} have "
                "verified evidence_locations — routing to DEGRADED (best-effort) "
                "patch instead of EVIDENCE_INCOMPLETE.",
                flush=True,
            )
            memory.record_action(
                phase="closure-check",
                subagent="closure-checker",
                outcome=f"degraded_patch:grounded={len(grounded)}",
            )
            return PipelineState.CLOSED
        print(
            "[orchestrator] closure not approved and no grounded requirements "
            "(zero verified evidence_locations) — skipping patch as "
            "EVIDENCE_INCOMPLETE rather than guessing.",
            flush=True,
        )
        patch_outcome = "EVIDENCE_INCOMPLETE"
        return PipelineState.CLOSURE_FORCED_FAIL

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
            target = _pick_next_requirement(current_evidence, _ds_frozen_reqs) if current_evidence else None
            adaptive_package = None
            adaptive_tasks: list[str] = []
            batch_mode = os.environ.get("DEEP_SEARCH_BATCH_MODE", "single").strip().lower()
            if batch_mode not in {"single", "adaptive"}:
                raise ValueError("DEEP_SEARCH_BATCH_MODE must be single or adaptive")
            if current_evidence is not None and batch_mode == "adaptive":
                unchecked_items = [
                    req for req in current_evidence.requirements
                    if req.verdict == "UNCHECKED" and req.id not in _ds_frozen_reqs
                ]
                anchor_index = build_anchor_index(current_evidence, repo_dir)
                adaptive_package = next(
                    (
                        package
                        for package in create_work_packages(unchecked_items, anchor_index)
                        if len(package.requirement_ids) > 1
                    ),
                    None,
                )
                if adaptive_package is not None:
                    by_id = {req.id: req for req in unchecked_items}
                    target = by_id[adaptive_package.requirement_ids[0]]
                    adaptive_tasks = [
                        _build_deep_search_todo(
                            by_id[rid], rework_context=by_id[rid].rework_context,
                        )
                        for rid in adaptive_package.requirement_ids
                    ]

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
                unchecked_without_target = check_sufficiency(current_evidence)
                if unchecked_without_target:
                    # The picker intentionally excludes frozen requirements.
                    # If a later gate reset one to UNCHECKED, bouncing between
                    # UNDER_SPECIFIED and EVIDENCE_REFINING cannot make progress:
                    # no model call occurs, so the normal counter never moves.
                    # Fail safely through the one forced closure pass instead.
                    print(
                        "[orchestrator] no selectable deep-search target but "
                        f"unchecked requirements remain {unchecked_without_target[:5]} "
                        "— forcing final closure evaluation.",
                        flush=True,
                    )
                    budget.force_exhausted()
                    memory.record_action(
                        phase="deep-search",
                        subagent="deep-search",
                        outcome="forced_closure:no_selectable_unchecked_target",
                    )
                else:
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

            # Arm a forced-Read directive when the PRIOR round(s) for this
            # requirement were hollow (asserted a verdict but landed zero
            # evidence_locations).  This breaks the 004/011 spin where the agent
            # repeatedly claims Reads it never performed.
            force_read = ""
            if _ds_hollow_counts.get(target.id, 0) >= _DS_HOLLOW_FORCE_READ:
                force_read = (
                    "⚠ MANDATORY READ-FIRST DIRECTIVE ⚠\n"
                    f"A prior investigation of {target.id} returned a verdict but "
                    "cited ZERO evidence_locations that survived traceability "
                    "checks — i.e. it asserted a conclusion without grounding it "
                    "in code actually opened.\n"
                    "Before deciding any verdict this round you MUST:\n"
                    "1. Use Glob/Grep to locate the file(s) named in the "
                    "requirement text.\n"
                    "2. Use Read to open each candidate region and confirm the "
                    "exact lines with your own eyes.\n"
                    "3. Cite every evidence_location as `path:line` ONLY for "
                    "lines you actually Read this round. Do not carry over or "
                    "infer locations from prior rounds or from the requirement "
                    "prose.\n"
                    "A verdict with an empty evidence_locations list will be "
                    "rejected again — grounding is the whole task."
                )
            todo_task = _build_deep_search_todo(
                target,
                rework_context=target.rework_context,
                force_read_directive=force_read,
            )
            # Inject the rendered SharedWorkingMemory section (LTM summaries,
            # custom repair discipline, build-error feedback) so the deep-search
            # agent actually sees the same context the orchestrator gathered.
            working_memory_block = memory.format_for_deep_search(target)
            print(
                f"[orchestrator] Dispatching deep-search for {target.id}: "
                f"{target.text[:80]!r}",
                flush=True,
            )
            try:
                additional_reports = []
                if adaptive_package and len(adaptive_package.requirement_ids) > 1:
                    package_report = await asyncio.wait_for(
                        _run_adaptive_deep_search_async(
                            adaptive_package, adaptive_tasks, current_evidence,
                            repo_dir, working_memory_block=working_memory_block,
                        ), timeout=deep_search_timeout_s,
                    )
                    completed = {
                        item.requirement_id: item.scoped_report
                        for item in package_report.requirement_results
                    }
                    report = completed.get(target.id)
                    additional_reports = [
                        item for rid, item in completed.items() if rid != target.id
                    ]
                    if report is None:
                        print(
                            f"[orchestrator] adaptive package left {target.id} "
                            "unresolved; falling back to single-requirement search",
                            flush=True,
                        )
                        report = await asyncio.wait_for(
                            _run_deep_search_async(
                                todo_task, current_evidence, repo_dir,
                                working_memory_block=working_memory_block,
                            ), timeout=deep_search_timeout_s,
                        )
                else:
                    report = await asyncio.wait_for(
                        _run_deep_search_async(
                            todo_task, current_evidence, repo_dir,
                            working_memory_block=working_memory_block,
                        ), timeout=deep_search_timeout_s,
                    )
            except ModelInfrastructureError:
                # Provider/relay outages are not evidence failures.  Preserve
                # the last durable checkpoint and let the runner retry later.
                raise
            except (Exception, asyncio.TimeoutError) as exc:
                budget.record_iteration()
                reason = (
                    f"timeout>{deep_search_timeout_s}s"
                    if isinstance(exc, asyncio.TimeoutError)
                    else f"{type(exc).__name__}: {exc}"
                )
                write_event({
                    "component": "deep-search-timeout" if isinstance(exc, asyncio.TimeoutError) else "deep-search-error",
                    "model": model_label(), "requirement_ids": [target.id],
                    "call_reason": "timeout" if isinstance(exc, asyncio.TimeoutError) else "structured_retry",
                    "exception": reason,
                })
                print(
                    "[orchestrator] deep-search failed for "
                    f"{target.id}: {reason}",
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
            for extra_report in additional_reports:
                await _persist_report_findings(
                    extra_report, scope_requirement_id=extra_report.target_requirement_id,
                )
                memory.record_action(
                    phase="deep-search", subagent="deep-search-adaptive",
                    outcome=f"iter{budget.iteration}:{extra_report.requirement_verdict}",
                    requirement_id=extra_report.target_requirement_id,
                )
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

            # Progress-stall breaker (improvement 2): detect a requirement that
            # is being re-investigated round after round without ever gaining a
            # verified evidence_location.  This is the 004/011 spin: deep-search
            # claims a verdict each round, but token-traceability / attribution
            # strips the locations to empty, the verdict reverts, and the loop
            # repeats for all 30 iterations.
            #
            # Signal note: retrieved_code is NOT usable here — cache_retrieved_code
            # is wired to no agent (run_structured_query registers no MCP server),
            # so it is empty even on productive rounds (verified: issue 009 had 20
            # real locations yet retrieved_code == {}).  The only signal that
            # actually separates productive (009) from hollow (004/011) rounds is
            # the count of persisted evidence_locations.
            persisted_locs = (
                len(target_after.evidence_locations) if target_after else 0
            )
            persisted_verdict = (
                target_after.verdict if target_after else "UNCHECKED"
            )
            # A round is "hollow" when it asserts an actionable verdict but lands
            # zero evidence_locations — i.e. it did no grounding that survived.
            hollow_round = persisted_locs == 0
            if hollow_round:
                _ds_hollow_counts[target.id] = (
                    _ds_hollow_counts.get(target.id, 0) + 1
                )
            else:
                _ds_hollow_counts[target.id] = 0

            sig = (persisted_verdict, persisted_locs)
            prev_sig = _ds_stall_signatures.get(target.id)
            # Progress = more evidence_locations than before, or first time we
            # reach a non-UNCHECKED verdict that actually carries locations.
            improved = (
                prev_sig is None
                or sig[1] > prev_sig[1]
            )
            _ds_stall_signatures[target.id] = sig
            if improved:
                _ds_stall_counts[target.id] = 0
            else:
                _ds_stall_counts[target.id] = _ds_stall_counts.get(target.id, 0) + 1
                if (
                    _ds_stall_counts[target.id] >= _DS_STALL_MAX
                    and target.id not in _ds_frozen_reqs
                ):
                    _ds_frozen_reqs.add(target.id)
                    # Ensure it carries SOME non-UNCHECKED verdict so the
                    # sufficiency gate stops bouncing the pipeline back here.
                    if target_after and target_after.verdict == "UNCHECKED":
                        target_after.verdict = (
                            report.requirement_verdict or "TO_BE_PARTIAL"
                        )
                        if not target_after.findings:
                            target_after.findings = (
                                "[frozen: deep-search stalled — no verified "
                                f"evidence_locations after {_ds_stall_counts[target.id]} visits]"
                            )
                    print(
                        f"[orchestrator] {target.id} frozen after "
                        f"{_ds_stall_counts[target.id]} stalled visits "
                        f"(zero verified evidence_locations gained) — removing "
                        f"from rework pool to preserve budget for patch planning.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="deep-search",
                        subagent="deep-search",
                        outcome=f"frozen_stalled:{_ds_stall_counts[target.id]}_visits",
                        requirement_id=target.id,
                    )

            # ── Checkpoint ①: after each completed deep-search round ────
            _save_checkpoint(
                output_dir, PipelineState.UNDER_SPECIFIED, memory,
                _pack_counters(
                    budget, rework_rounds_used, patch_verify_rounds_used,
                    plan_coverage_rounds_used, per_req_unchecked_count,
                    closure_failure_streak, rework_rounds_by_req,
                ),
                ltm_query, custom_route_query,
                aggregate_patch_plan=aggregate_patch_plan,
            )

            # Drain the pending investigation queue in-place. Merely having
            # another UNCHECKED item is queue progress, not a sufficiency
            # failure and should not grow closure/prompt history.
            refreshed = get_submitted_evidence()
            next_target = (
                _pick_next_requirement(refreshed, _ds_frozen_reqs)
                if refreshed is not None else None
            )
            if next_target is not None and not budget.is_exhausted():
                memory.record_action(
                    phase="deep-search", subagent="deep-search",
                    outcome="queue_progress", requirement_id=next_target.id,
                )
                continue
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
                reset_ids_attr: list[str] = []
                for rid in bad_attribution:
                    if rid in _ds_frozen_reqs:
                        print(
                            f"[orchestrator] attribution gate: {rid} failed "
                            "but is frozen — skipping reset (would loop).",
                            flush=True,
                        )
                        continue
                    if reset_requirement_for_rework(
                        rid,
                        audit_feedback="Attribution check failed: non-compliant verdict "
                        "requires valid evidence_locations (path:LINE or path:LINE-LINE).",
                    ):
                        reset_ids_attr.append(rid)
                if reset_ids_attr:
                    memory.record_action(
                        phase="evidence-refining",
                        outcome=(
                            f"attribution_failed:{len(reset_ids_attr)}_missing_locations"
                        ),
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
                    f"{anchor_format_bad[:3]}. Forcing final closure instead of "
                    "bouncing without a reset target.",
                    flush=True,
                )
                memory.record_action(
                    phase="evidence-refining",
                    outcome=f"anchor_format_failed:{len(anchor_format_bad)}_bad",
                )
                budget.force_exhausted()

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
                    if rid in _ds_frozen_reqs:
                        print(
                            f"[orchestrator] consistency-anchor gate: {rid} failed "
                            "but is frozen — skipping reset (would loop).",
                            flush=True,
                        )
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
                    # Never reset a frozen requirement — it stalled because deep-search
                    # could not produce verified evidence for it. Resetting it back to
                    # UNCHECKED would restart the same infinite loop.
                    if rid in _ds_frozen_reqs:
                        print(
                            f"[orchestrator] static-grounding gate: {rid} failed "
                            f"but is frozen — skipping reset (would loop).",
                            flush=True,
                        )
                        continue
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
                    if rid in _ds_frozen_reqs:
                        print(
                            f"[orchestrator] structural I2 gate: {rid} failed "
                            "but is frozen — skipping reset (would loop).",
                            flush=True,
                        )
                        continue
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
            closure_validation_feedback = ""
            max_attempts = closure_retry_limit + 1
            for attempt in range(1, max_attempts + 1):
                try:
                    verdict = await _run_closure_checker_async(
                        current_evidence,
                        manifest,
                        repo_dir,
                        validation_feedback=closure_validation_feedback,
                    )
                    closure_exc = None
                    break
                except ModelInfrastructureError:
                    # Never consume closure semantic retries or mutate the
                    # closure failure streak for an infrastructure outage.
                    raise
                except Exception as exc:
                    closure_exc = exc
                    closure_validation_feedback = f"{type(exc).__name__}: {exc}"
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
                    assert is_valid_transition(
                        state, PipelineState.CLOSURE_FORCED_FAIL
                    )
                    assert is_valid_transition(state, PipelineState.CLOSED)
                    state = _route_forced_fail()
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
                    dimension_findings=list(verdict.dimension_findings) + [
                        DimensionFinding(
                            dimension="sufficiency", status="FAIL",
                            requirement_ids=[rid],
                            conflicting_field="findings",
                            explanation="Required prescriptive audit result is missing.",
                        ) for rid in uncovered
                    ],
                    missing=list(verdict.missing) + [coverage_msg]
                        + [f"uncovered req {rid}" for rid in uncovered],
                    suggested_tasks=list(
                        dict.fromkeys(list(verdict.suggested_tasks) + uncovered)
                    ),
                    conflicts=verdict.conflicts,
                    shared_fact_gaps=verdict.shared_fact_gaps,
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
                # ── Checkpoint ②: evidence closure approved ─────────────
                _save_checkpoint(
                    output_dir, PipelineState.CLOSED, memory,
                    _pack_counters(
                        budget, rework_rounds_used, patch_verify_rounds_used,
                        plan_coverage_rounds_used, per_req_unchecked_count,
                        closure_failure_streak, rework_rounds_by_req,
                    ),
                    ltm_query, custom_route_query,
                    aggregate_patch_plan=aggregate_patch_plan,
                )
                if stop_after_closure:
                    print(
                        "[orchestrator] Analysis stage complete; preserving CLOSED "
                        "checkpoint for generation stage.",
                        flush=True,
                    )
                    return evidence_path
            else:
                closure_diagnostics = list(verdict.missing[:3])
                if not closure_diagnostics:
                    closure_diagnostics.extend(
                        f"{finding.dimension} FAIL "
                        f"({','.join(finding.requirement_ids) or '<no-req>'}; "
                        f"field={finding.conflicting_field or 'evidence'})"
                        for finding in verdict.dimension_findings
                        if finding.status == "FAIL"
                    )
                if verdict.conflicts:
                    closure_diagnostics.extend(
                        f"conflict {edge.left_requirement_id}<->{edge.right_requirement_id} "
                        f"on {edge.conflicting_field}; recheck={edge.recommended_recheck_side}"
                        for edge in verdict.conflicts
                    )
                print(
                    f"[orchestrator] EVIDENCE_MISSING: "
                    f"{'; '.join(closure_diagnostics[:4]) or '<no structured diagnostic>'}",
                    flush=True,
                )
                memory.record_action(
                    phase="closure-check",
                    subagent="closure-checker",
                    outcome="EVIDENCE_MISSING" + ("_forced" if forced else ""),
                )
                if closure_only_retry and not forced:
                    targeted_ids = list(verdict.suggested_tasks)
                    targeted_ids.extend(
                        rid
                        for finding in verdict.dimension_findings
                        if finding.status == "FAIL"
                        for rid in finding.requirement_ids
                    )
                    targeted_ids.extend(_extract_req_ids(list(verdict.missing)))
                    targeted_ids = list(dict.fromkeys(
                        rid for rid in targeted_ids if rid
                    ))
                    print(
                        "[orchestrator] closure-only retry received "
                        "EVIDENCE_MISSING; preserving prior evidence and "
                        "opening one bounded targeted evidence pass for "
                        f"{targeted_ids or ['<most-blocking>']}.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="closure-check",
                        subagent="closure-checker",
                        outcome="closure_only_retry_targeted:EVIDENCE_MISSING",
                    )
                    # The preserved checkpoint commonly exhausted its original
                    # budget.  Grant exactly one additional search per named
                    # blocking requirement, then use the normal bounded rework
                    # path.  Disable this branch so it cannot recursively grant
                    # another recovery round.
                    extra_rounds = max(1, len(targeted_ids))
                    budget.max_iterations = max(
                        budget.max_iterations,
                        budget.iteration + extra_rounds,
                    )
                    for rid in targeted_ids:
                        rework_rounds_by_req[rid] = min(
                            rework_rounds_by_req.get(rid, 0),
                            per_req_rework_rounds_max - 1,
                        )
                        per_req_unchecked_count[rid] = min(
                            per_req_unchecked_count.get(rid, 0),
                            per_req_unchecked_max - 1,
                        )
                    closure_only_retry = False
                if forced:
                    # Budget already exhausted and closure still failed —
                    # do NOT loop back to deep-search. Route to degraded patch
                    # if grounded, else terminate as EVIDENCE_INCOMPLETE.
                    assert is_valid_transition(
                        state, PipelineState.CLOSURE_FORCED_FAIL
                    )
                    assert is_valid_transition(state, PipelineState.CLOSED)
                    state = _route_forced_fail()
                else:
                    # Rework path (phase 25): derive per-requirement specs from
                    # the closure-checker's dimension findings.  sufficiency
                    # FAIL → deepen (full reset); consistency / prescriptive
                    # FAIL → reconcile (cross-req full reset or findings-only).
                    rework_specs = _derive_rework_specs(verdict)
                    # Req ids cited in missing/suggested_tasks but absent from
                    # the dimension findings (e.g. coverage-fail synthesized ids)
                    # have no spec → fall back to a deepen full reset.
                    # Free-text missing/suggested_tasks are diagnostic only.
                    # Rework scope must come from structured dimension findings
                    # or validated conflict edges; never regex-reset ids from prose.

                    raw_conflict_req_ids = list(rework_specs.keys())
                    conflict_req_ids, frozen_req_ids, capped_req_ids = (
                        _eligible_closure_rework_ids(
                            raw_conflict_req_ids,
                            _ds_frozen_reqs,
                            rework_rounds_by_req,
                            per_req_rework_rounds_max,
                        )
                    )
                    if frozen_req_ids:
                        print(
                            "[orchestrator] excluding frozen (stalled) reqs from "
                            f"rework pool: {frozen_req_ids}",
                            flush=True,
                        )
                    if capped_req_ids:
                        print(
                            "[orchestrator] excluding reqs that reached per-req "
                            f"closure rework cap {per_req_rework_rounds_max}: "
                            f"{capped_req_ids}",
                            flush=True,
                        )
                    if conflict_req_ids:
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
                        for rid in reset_ids:
                            rework_rounds_by_req[rid] = (
                                rework_rounds_by_req.get(rid, 0) + 1
                            )
                        scope_note = ", ".join(
                            f"{rid}:{rework_specs[rid].operator}="
                            f"{'findings' if rework_specs[rid].fields_to_reset == {'findings'} else 'full'}"
                            f":req_round={rework_rounds_by_req.get(rid, 0)}/{per_req_rework_rounds_max}"
                            for rid in reset_ids
                        )
                        print(
                            "[orchestrator] EVIDENCE_MISSING → rework: "
                            f"re-opened {reset_ids} [{scope_note}] "
                            f"(total_rounds={rework_rounds_used})",
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
                                f"total_rounds={rework_rounds_used}"
                            ),
                        )
                        assert is_valid_transition(state, PipelineState.UNDER_SPECIFIED)
                        state = PipelineState.UNDER_SPECIFIED
                    else:
                        reason = (
                            "no parseable req IDs in closure output"
                            if not raw_conflict_req_ids
                            else "per-req rework cap exhausted"
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
                        assert is_valid_transition(
                            state, PipelineState.CLOSURE_FORCED_FAIL
                        )
                        assert is_valid_transition(state, PipelineState.CLOSED)
                        state = _route_forced_fail()

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
            # On a repatch round (entered from PATCH_VERIFYING), allow the
            # planner to return None instead of crashing the whole run when the
            # SDK yields success-but-empty structured_output (issue 010). The
            # first plan keeps the hard guarantee (allow_none stays False) since
            # there is no prior patch to fall back on. A None here degrades to
            # BUILD_FAILED below, preserving the last applied patch.diff.
            is_repatch = patch_verify_rounds_used > 0
            plan = await _run_patch_planner_async(
                memory, repo_dir, allow_none=is_repatch
            )
            # The sliced-evidence view was consumed by the planner prompt just
            # built; clear it so any later full-context consumer (or a
            # subsequent planning pass) sees the complete evidence again.
            memory.evidence_focus_files = []
            if plan is None:
                print(
                    "[orchestrator] patch-planner returned no plan on repatch "
                    f"(round {patch_verify_rounds_used}); the prior patch is "
                    "already applied. Terminating as BUILD_FAILED rather than "
                    "crashing — run docker eval for the real verdict.",
                    flush=True,
                )
                memory.record_action(
                    phase="patch-planning",
                    subagent="patch-planner",
                    outcome="repatch_no_structured_output:BUILD_FAILED",
                )
                patch_outcome = "BUILD_FAILED"
                assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                state = PipelineState.PATCH_FAILED
                continue
            memory.record_action(
                phase="patch-planning",
                subagent="patch-planner",
                outcome=f"{len(plan.edits)}_files_planned",
            )

            # Spec-priority firewall (issue 011): every AS_IS_VIOLATED
            # requirement owns a concrete change at its cited location. If the
            # plan touches none of a violated req's cited files, the prescribed
            # fix is being skipped — re-plan once with an explicit coverage
            # demand fed back to the planner. Capped so a planner that
            # genuinely cannot cover it (e.g. the citation was wrong) still
            # makes progress instead of looping.
            uncovered = check_plan_covers_violations(memory.evidence_cards, plan)
            if uncovered and plan_coverage_rounds_used < plan_coverage_rounds_max:
                plan_coverage_rounds_used += 1
                coverage_msg = render_plan_coverage_feedback(
                    memory.evidence_cards, uncovered
                )
                memory.build_error_feedback = (
                    (memory.build_error_feedback + "\n\n" + coverage_msg)
                    if memory.build_error_feedback else coverage_msg
                )
                print(
                    f"[orchestrator] plan-coverage gap on {uncovered}; re-planning "
                    f"(round {plan_coverage_rounds_used}/{plan_coverage_rounds_max}).",
                    flush=True,
                )
                memory.record_action(
                    phase="patch-planning",
                    subagent="patch-planner",
                    outcome=f"plan_coverage_gap:reopen={len(uncovered)}_reqs",
                )
                # Stay in CLOSED to re-dispatch the planner with the feedback.
                continue
            elif uncovered:
                print(
                    f"[orchestrator] plan-coverage gap on {uncovered} persists after "
                    "re-plan; proceeding (planner could not cover — likely a bad "
                    "citation, surfaced for the docker eval).",
                    flush=True,
                )

            assert is_valid_transition(state, PipelineState.PATCH_PLANNING)
            state = PipelineState.PATCH_PLANNING

        # ── PatchPlanning: dispatch patch-generator ───────────────────
        elif state == PipelineState.PATCH_PLANNING:
            # Accumulate planned files across rounds so the final git-diff
            # promotes every created file even after a repatch overwrites
            # memory.patch_plan with the fixup plan.
            if memory.patch_plan is not None:
                aggregate_patch_plan = _merge_patch_plans(
                    aggregate_patch_plan, memory.patch_plan
                )
                all_planned_files.update(e.filepath for e in memory.patch_plan.edits)

            print("[orchestrator] Dispatching patch-generator...", flush=True)
            try:
                success = await _run_patch_generator_async(memory, repo_dir, output_dir)
            except PatchGeneratorInfraError as exc:
                memory.record_action(
                    phase="patch-generation",
                    subagent="patch-generator",
                    outcome="MODEL_INFRA_FAILURE",
                )
                patch_outcome = "MODEL_INFRA_FAILURE"
                print(f"[patch-generator] infrastructure failure: {exc}", flush=True)
                assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                state = PipelineState.PATCH_FAILED
                continue
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
            # Test files belong to the evaluator (it applies its own test
            # patch on top of ours). Any model edit to a test file is at best
            # ignored and at worst shadows the gold tests (issue 002). Revert
            # them before anything else so neither the build gate nor the
            # final diff sees model-authored test changes.
            reverted_tests = revert_test_file_edits(repo_dir, base_commit=None)
            if reverted_tests:
                print(
                    f"[build-verify] reverted {len(reverted_tests)} model edit(s) "
                    f"to test files (evaluator owns tests): "
                    f"{', '.join(reverted_tests[:8])}"
                    + (" ..." if len(reverted_tests) > 8 else ""),
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome=f"test_edits_reverted:{len(reverted_tests)}",
                )

            round_verification_plan = _merge_patch_plans(
                aggregate_patch_plan, memory.patch_plan
            )
            current_plan_files = [
                edit.filepath
                for edit in round_verification_plan.edits
            ] if round_verification_plan is not None else []
            artifact_diff_text = _collect_git_diff(
                repo_dir,
                planned_files=sorted(all_planned_files | set(current_plan_files)),
            )
            artifact_result = verify_patch_artifacts(
                repo_dir,
                round_verification_plan,
                artifact_diff_text,
            )
            artifact_verify_log.append(
                {
                    "round": patch_verify_rounds_used,
                    **artifact_result.to_log(),
                }
            )
            print(
                "[artifact-verify] "
                + (
                    "clean."
                    if artifact_result.ok
                    else f"{len(artifact_result.findings)} finding(s)."
                ),
                flush=True,
            )
            if not artifact_result.ok:
                if artifact_result.empty_patch:
                    memory.record_action(
                        phase="artifact-verify", outcome="NO_EFFECT_PATCH_terminal"
                    )
                    patch_outcome = "NO_EFFECT_PATCH"
                    assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                    state = PipelineState.PATCH_FAILED
                    continue
                if artifact_repair_rounds_used < artifact_repair_rounds_max:
                    artifact_repair_rounds_used += 1
                    artifact_errors = _artifact_findings_to_errors(
                        repo_dir, artifact_result.findings
                    )
                    memory.build_error_feedback = (
                        "Patch artifact verification failed. These structural "
                        "findings are blocking Stage2 artifacts; fix production "
                        "code or plan/diff coverage, do not edit tests, and do "
                        "not bypass the gate.\n\n"
                        f"{render_artifact_feedback(artifact_result.findings, repo_dir)}"
                    )
                    if artifact_errors:
                        repair_context_plan = _merge_patch_plans(
                            aggregate_patch_plan, memory.patch_plan
                        )
                        pruned_plan, dropped_edits = _prune_plan_to_error_files(
                            repair_context_plan, artifact_errors
                        )
                        memory.patch_plan = _augment_repair_plan_with_errors(
                            pruned_plan,
                            artifact_errors,
                            reason="patch artifact verification gate",
                        )
                        if memory.patch_plan is not None:
                            aggregate_patch_plan = _merge_patch_plans(
                                aggregate_patch_plan, memory.patch_plan
                            )
                            all_planned_files.update(
                                edit.filepath for edit in memory.patch_plan.edits
                            )
                        memory.evidence_focus_files = sorted({
                            e.file.replace("\\", "/").strip().lstrip("./")
                            for e in artifact_errors
                            if e.file and e.file != "(build)"
                        })
                    else:
                        dropped_edits = 0
                        memory.evidence_focus_files = []
                    artifact_verify_log[-1]["repair_triggered"] = True
                    artifact_verify_log[-1]["repair_round"] = artifact_repair_rounds_used
                    artifact_verify_log[-1]["repair_focus_files"] = list(
                        memory.evidence_focus_files
                    )
                    artifact_verify_log[-1]["dropped_plan_edits"] = dropped_edits
                    print(
                        "[artifact-verify] failed; running direct artifact "
                        f"repair round {artifact_repair_rounds_used}/"
                        f"{artifact_repair_rounds_max} before build/static gates.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="artifact-repair",
                        outcome=f"direct_repair:{len(artifact_result.findings)}",
                    )
                    try:
                        repaired = await _run_patch_generator_async(
                            memory, repo_dir, output_dir
                        )
                    except PatchGeneratorInfraError as exc:
                        artifact_verify_log[-1]["repair_failed"] = True
                        artifact_verify_log[-1]["infra_failure"] = str(exc)
                        patch_outcome = "MODEL_INFRA_FAILURE"
                        print(f"[patch-generator] infrastructure failure: {exc}", flush=True)
                        assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                        state = PipelineState.PATCH_FAILED
                        continue
                    if not repaired:
                        artifact_verify_log[-1]["repair_failed"] = True
                        patch_outcome = "PATCH_INCOMPLETE"
                        assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                        state = PipelineState.PATCH_SUCCESS
                        continue
                    continue
                print(
                    "[artifact-verify] still failing after direct repair; patch "
                    "will not be accepted as a usable Stage2 artifact.",
                    flush=True,
                )
                memory.record_action(
                    phase="artifact-verify",
                    outcome=f"artifact_gate_failed:{len(artifact_result.findings)}",
                )
                patch_outcome = "PATCH_INCOMPLETE"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            system = detect_build_system(repo_dir)
            print(
                f"[build-verify] build system detected: {system}",
                flush=True,
            )

            # Heuristic git-only gates (phase 27). These run regardless of
            # build system — they are the ONLY line of defense on JS/unknown
            # repos and on hosts where the toolchain is unavailable, exactly
            # the paths where issues 001/008/009/010 slipped through.
            contract_drift = check_contract_drift(repo_dir, base_commit=None)
            parallel_impl = check_parallel_impl_consistency(repo_dir, base_commit=None)
            removed_sym_refs = check_removed_symbol_test_refs(repo_dir, base_commit=None)
            go_unexport = check_go_unexport_consistency(repo_dir, base_commit=None)
            config_shape = check_config_entry_shape(repo_dir, base_commit=None)
            python_noniterable = check_python_noniterable_class_loop(
                repo_dir, base_commit=None
            )
            python_helper_api = check_python_helper_api_usage(
                repo_dir, base_commit=None
            )
            python_config_subscript = check_python_config_subscript_fallback(
                repo_dir, base_commit=None
            )
            python_moved_class_dunder = check_python_moved_class_dunder_methods(
                repo_dir, base_commit=None
            )
            for label, errs in (
                ("contract-drift", contract_drift),
                ("parallel-impl", parallel_impl),
                ("removed-symbol-test-refs", removed_sym_refs),
                ("go-unexport", go_unexport),
                ("config-entry-shape", config_shape),
                ("python-noniterable-class-loop", python_noniterable),
                ("python-helper-api", python_helper_api),
                ("python-config-subscript", python_config_subscript),
                ("python-moved-class-dunder", python_moved_class_dunder),
            ):
                print(
                    f"[build-verify] {label} gate: "
                    + (f"{len(errs)} finding(s)." if errs else "clean."),
                    flush=True,
                )

            heuristic_errors = (
                list(contract_drift) + list(parallel_impl)
                + list(removed_sym_refs) + list(go_unexport)
                + list(config_shape) + list(python_noniterable)
                + list(python_helper_api) + list(python_config_subscript)
                + list(python_moved_class_dunder)
            )
            if heuristic_errors:
                memory.record_action(
                    phase="build-verify",
                    outcome=f"heuristic_warnings:{len(heuristic_errors)}",
                )

            if system in ("node", "java", "unknown"):
                if heuristic_errors:
                    build_verify_log.append(
                        {"system": system, "outcome": "STATIC_GATE_FAILED",
                         "reverted_tests": reverted_tests,
                         "static_warnings": _errs_to_log(heuristic_errors)}
                    )
                    memory.record_action(
                        phase="build-verify",
                        outcome=f"static_gate_failed:{len(heuristic_errors)}",
                    )
                    patch_outcome = "BUILD_FAILED_NO_REPAIR"
                    assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                    state = PipelineState.PATCH_SUCCESS
                    continue
                build_verify_log.append(
                    {"system": system, "outcome": "SKIPPED",
                     "reverted_tests": reverted_tests,
                     "heuristic_warnings": _errs_to_log(heuristic_errors)}
                )
                memory.record_action(phase="build-verify", outcome=f"skipped:{system}")
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            python_targets = (
                changed_python_production_files(repo_dir)
                if system == "python" else None
            )
            go_targets = (
                changed_go_packages(repo_dir) if system == "go" else None
            )
            post = run_build_check(
                repo_dir,
                system,
                python_targets=python_targets,
                go_targets=go_targets,
            )
            print(
                f"[build-verify] post-patch: ok={post.ok} "
                f"errors={len(post.errors)} timed_out={post.timed_out} "
                f"unverifiable={post.unverifiable} cmd='{post.command}'",
                flush=True,
            )

            # Rename-residue gate (phase 23) and undefined-config-symbol gate
            # (phase 24): git-only, independent of build outcome.
            residues = check_rename_residue(repo_dir, base_commit=None)
            print(
                f"[build-verify] rename-residue gate: "
                + (f"{len(residues)} unupdated old-symbol references."
                   if residues else "clean."),
                flush=True,
            )
            config_sym_errors = check_undefined_config_symbol(
                repo_dir, base_commit=None
            )
            print(
                f"[build-verify] undefined-config-symbol gate: "
                + (f"{len(config_sym_errors)} unresolved reference(s)."
                   if config_sym_errors else "clean."),
                flush=True,
            )

            static_warnings = list(residues) + list(config_sym_errors) + heuristic_errors

            if static_warnings:
                if patch_verify_rounds_used < 1:
                    patch_verify_rounds_used += 1
                    feedback = _render_heuristic_feedback(
                        contract_drift,
                        parallel_impl,
                        removed_sym_refs,
                        go_unexport,
                        config_shape,
                        python_noniterable,
                        python_helper_api,
                        python_config_subscript,
                        python_moved_class_dunder,
                        static_warnings,
                        residues=residues,
                        config_sym_errors=config_sym_errors,
                    )
                    if not feedback:
                        feedback = render_errors_for_feedback(static_warnings)
                    removed_defs = _enrich_removed_symbol_errors_with_base_definitions(
                        repo_dir, static_warnings
                    )
                    if removed_defs:
                        feedback += "\n\n" + removed_defs
                    memory.build_error_feedback = (
                        "Static patch-closure gate failed. These findings are "
                        "blocking Stage2 artifacts; fix production code, do "
                        "not edit tests, and do not weaken or bypass the gate.\n\n"
                        f"{feedback}"
                    )
                    repair_context_plan = _merge_patch_plans(
                        aggregate_patch_plan, memory.patch_plan
                    )
                    static_repair_errors = _expand_config_symbol_owner_context(
                        repo_dir, repair_context_plan, static_warnings
                    )
                    pruned_plan, dropped_edits = _prune_plan_to_error_files(
                        repair_context_plan, static_repair_errors
                    )
                    memory.patch_plan = _augment_repair_plan_with_errors(
                        pruned_plan,
                        static_repair_errors,
                        reason="static patch-closure gate",
                    )
                    if memory.patch_plan is not None:
                        aggregate_patch_plan = _merge_patch_plans(
                            aggregate_patch_plan, memory.patch_plan
                        )
                        all_planned_files.update(
                            edit.filepath for edit in memory.patch_plan.edits
                        )
                    focus_files = sorted({
                        e.file.replace("\\", "/").strip().lstrip("./")
                        for e in static_repair_errors
                        if e.file and e.file != "(build)"
                    })
                    memory.evidence_focus_files = focus_files
                    build_verify_log.append(
                        {
                            "system": system,
                            "outcome": "STATIC_GATE_REPAIR",
                            "command": post.command,
                            "python_targets": python_targets or [],
                            "reverted_tests": reverted_tests,
                            "static_warnings": _errs_to_log(static_warnings),
                            "repair_triggered": True,
                            "dropped_plan_edits": dropped_edits,
                        }
                    )
                    print(
                        "[build-verify] static gate failed; running the single "
                        "direct static repair before accepting/rejecting Stage2.",
                        flush=True,
                    )
                    memory.record_action(
                        phase="static-gate-repair",
                        outcome=f"direct_repair:{len(static_warnings)}",
                    )
                    try:
                        repaired = await _run_patch_generator_async(memory, repo_dir, output_dir)
                    except PatchGeneratorInfraError as exc:
                        build_verify_log[-1]["outcome"] = "MODEL_INFRA_FAILURE"
                        build_verify_log[-1]["infra_failure"] = str(exc)
                        patch_outcome = "MODEL_INFRA_FAILURE"
                        print(f"[patch-generator] infrastructure failure: {exc}", flush=True)
                        assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                        state = PipelineState.PATCH_FAILED
                        continue
                    if not repaired:
                        build_verify_log[-1]["outcome"] = "STATIC_GATE_REPAIR_FAILED"
                        patch_outcome = "BUILD_FAILED_NO_REPAIR"
                        assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                        state = PipelineState.PATCH_SUCCESS
                        continue
                    continue

                print(
                    "[build-verify] static gate failed; patch will not be "
                    "accepted as a usable Stage2 artifact.",
                    flush=True,
                )
                memory.record_action(
                    phase="build-verify",
                    outcome=f"static_gate_failed:{len(static_warnings)}",
                )
                build_verify_log.append(
                    {
                        "system": system,
                        "outcome": "STATIC_GATE_FAILED",
                        "command": post.command,
                        "python_targets": python_targets or [],
                        "reverted_tests": reverted_tests,
                        "static_warnings": _errs_to_log(static_warnings),
                    }
                )
                patch_outcome = "BUILD_FAILED_NO_REPAIR"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            if post.unverifiable:
                raw_tail = (post.raw_output or "")[-2000:]
                print(
                    "[build-verify] compile result is un-attributable; no model "
                    "repair will run. Official evaluation remains authoritative.",
                    flush=True,
                )
                memory.record_action(phase="build-verify", outcome="unverifiable")
                build_verify_log.append(
                    {"system": system, "outcome": "UNVERIFIABLE",
                     "command": post.command, "timed_out": post.timed_out,
                     "python_targets": python_targets or [],
                     "reverted_tests": reverted_tests,
                     "raw_output_tail": raw_tail,
                     "static_warnings": _errs_to_log(static_warnings)}
                )
                patch_outcome = "BUILD_UNVERIFIABLE"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            if post.ok:
                memory.record_action(phase="build-verify", outcome="ok")
                build_verify_log.append(
                    {"system": system, "outcome": "PASSED", "command": post.command,
                     "python_targets": python_targets or [],
                     "reverted_tests": reverted_tests,
                     "static_warnings": _errs_to_log(static_warnings)}
                )
                patch_outcome = "PATCH_SUCCESS"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            baseline = _compute_baseline_build(
                repo_dir, system, python_targets=python_targets,
                go_targets=go_targets,
            )
            new_errors = diff_new_errors(baseline, post)
            attributable = [
                err for err in new_errors
                if err.file not in {"", "(build)"} and err.line is not None
            ]
            build_verify_log.append(
                {
                    "system": system,
                    "outcome": "FAILED_NO_REPAIR" if not attributable else "FAILED",
                    "command": post.command,
                    "python_targets": python_targets or [],
                    "timed_out": post.timed_out,
                    "baseline_computed": baseline is not None,
                    "reverted_tests": reverted_tests,
                    "new_errors": _errs_to_log(new_errors),
                    "attributable_errors": _errs_to_log(attributable),
                    "static_warnings": _errs_to_log(static_warnings),
                }
            )

            if not attributable:
                print(
                    "[build-verify] no new file/line-attributable compile error; "
                    "skipping model repair.",
                    flush=True,
                )
                memory.record_action(phase="build-verify", outcome="failed_no_repair")
                patch_outcome = "BUILD_FAILED_NO_REPAIR"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS
                continue

            previous_error_counts = [
                len(entry.get("attributable_errors", []))
                for entry in build_verify_log[:-1]
                if entry.get("repair_triggered")
            ]
            allow_compile_repair = _should_run_compile_repair(
                patch_verify_rounds_used,
                patch_verify_rounds_max,
                len(attributable),
                previous_error_counts[-1] if previous_error_counts else None,
            )
            if allow_compile_repair:
                if patch_verify_rounds_used >= patch_verify_rounds_max:
                    patch_verify_rounds_max += 1
                    print(
                        "[build-verify] compile errors are converging; granting "
                        "one bounded bonus repair round.",
                        flush=True,
                    )
                patch_verify_rounds_used += 1
                build_verify_log[-1]["repair_triggered"] = True
                repair_context_plan = _merge_patch_plans(
                    aggregate_patch_plan, memory.patch_plan
                )
                repair_errors = _reroute_test_compile_errors_to_production_files(
                    repair_context_plan,
                    attributable,
                )
                repair_errors = _expand_go_same_package_repair_context(
                    repair_context_plan,
                    repair_errors,
                )
                repair_errors = _expand_go_cross_package_owner_context(
                    repo_dir,
                    repair_context_plan,
                    repair_errors,
                )
                memory.build_error_feedback = render_errors_for_feedback(repair_errors)
                enriched = _enrich_go_errors_with_definitions(repo_dir, repair_errors)
                if enriched:
                    memory.build_error_feedback += "\n\n" + enriched
                package_exports = _enrich_go_errors_with_package_exports(
                    repo_dir, repair_errors
                )
                if package_exports:
                    memory.build_error_feedback += "\n\n" + package_exports
                import_paths = _enrich_go_errors_with_module_import_paths(
                    repo_dir, repair_errors
                )
                if import_paths:
                    memory.build_error_feedback += "\n\n" + import_paths
                pruned_plan, dropped_edits = _prune_plan_to_error_files(
                    repair_context_plan, repair_errors
                )
                memory.patch_plan = _augment_repair_plan_with_errors(
                    pruned_plan,
                    repair_errors,
                    reason="focused compile gate",
                )
                if memory.patch_plan is not None:
                    aggregate_patch_plan = _merge_patch_plans(
                        aggregate_patch_plan, memory.patch_plan
                    )
                    all_planned_files.update(
                        edit.filepath for edit in memory.patch_plan.edits
                    )
                focus_files = sorted({
                    e.file.replace("\\", "/").strip().lstrip("./")
                    for e in repair_errors
                })
                memory.evidence_focus_files = focus_files
                print(
                    "[build-verify] running direct compile repair "
                    f"round {patch_verify_rounds_used}/{patch_verify_rounds_max}; "
                    "closure and patch-planner are bypassed.",
                    flush=True,
                )
                memory.record_action(
                    phase="compile-repair", outcome="direct_repair",
                )
                try:
                    repaired = await _run_patch_generator_async(memory, repo_dir, output_dir)
                except PatchGeneratorInfraError as exc:
                    build_verify_log[-1]["outcome"] = "MODEL_INFRA_FAILURE"
                    build_verify_log[-1]["infra_failure"] = str(exc)
                    patch_outcome = "MODEL_INFRA_FAILURE"
                    print(f"[patch-generator] infrastructure failure: {exc}", flush=True)
                    assert is_valid_transition(state, PipelineState.PATCH_FAILED)
                    state = PipelineState.PATCH_FAILED
                    continue
                if not repaired:
                    build_verify_log[-1]["outcome"] = "FAILED_AFTER_REPAIR"
                    patch_outcome = "BUILD_FAILED_AFTER_REPAIR"
                    assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                    state = PipelineState.PATCH_SUCCESS
                    continue
                # Re-enter mechanical verification once; the consumed boolean
                # prevents any further model repair.
                continue
            else:
                print(
                    "[build-verify] compile still fails after direct repair "
                    f"budget {patch_verify_rounds_used}/{patch_verify_rounds_max}; "
                    "continuing without a usable Stage2 artifact.",
                    flush=True,
                )
                memory.record_action(phase="build-verify", outcome="failed_after_repair")
                build_verify_log[-1]["outcome"] = "FAILED_AFTER_REPAIR"
                patch_outcome = "BUILD_FAILED_AFTER_REPAIR"
                assert is_valid_transition(state, PipelineState.PATCH_SUCCESS)
                state = PipelineState.PATCH_SUCCESS

    # ── Step 4: Post-pipeline finalization ─────────────────────────────
    print(f"[orchestrator] Pipeline finished: {state.value}", flush=True)

    closure_approved = (state in (PipelineState.PATCH_SUCCESS, PipelineState.PATCH_FAILED)
                        and last_verdict is not None
                        and last_verdict.verdict == "CLOSURE_APPROVED")

    if state == PipelineState.CLOSURE_FORCED_FAIL and patch_outcome is None:
        patch_outcome = "EVIDENCE_INCOMPLETE"

    # Degraded-patch relabel (improvement 3): a patch produced under
    # degraded_patch_mode was never closure-approved — it covers only the
    # grounded subset of requirements.  Surface that honestly as PARTIAL_PATCH
    # so eval/telemetry never mistakes a best-effort patch for a fully-verified
    # one.  A clean build still beats EVIDENCE_INCOMPLETE (which scores zero),
    # but it is not PATCH_SUCCESS.  BUILD_FAILED / PATCH_FAILED keep their own
    # (worse) labels — degraded mode only downgrades the success labels.
    if degraded_patch_mode and patch_outcome in (
        "PATCH_SUCCESS", "BUILD_UNVERIFIABLE",
    ):
        print(
            f"[orchestrator] degraded patch mode: relabeling {patch_outcome} "
            f"-> PARTIAL_PATCH (best-effort patch over grounded requirements; "
            f"closure was not approved).",
            flush=True,
        )
        patch_outcome = "PARTIAL_PATCH"

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

    if stop_after_closure:
        # A failed analysis stage is diagnostic-only. Never leave empty or
        # stale patch artifacts that a later collector could mistake for a
        # generated submission; main.py will reject the missing CLOSED
        # checkpoint and return a non-zero stage result.
        for stale_name in (
            "analysis_stage.json", "patch.diff", "prediction.json",
            "patch_plan.json", "patch_outcome.json", "artifact_verification.json",
        ):
            stale_path = output_dir / stale_name
            if stale_path.exists():
                stale_path.unlink()
        if state == PipelineState.CLOSURE_FORCED_FAIL and wm is not None:
            _save_checkpoint(
                output_dir,
                state,
                wm,
                _pack_counters(
                    budget,
                    rework_rounds_used,
                    patch_verify_rounds_used,
                    plan_coverage_rounds_used,
                    per_req_unchecked_count,
                    closure_failure_streak,
                    rework_rounds_by_req,
                ),
                ltm_query=ltm_query,
                custom_route_query=custom_route_query,
                aggregate_patch_plan=aggregate_patch_plan,
            )
            print(
                "[orchestrator] saved terminal ClosureForcedFail checkpoint "
                "with preserved budget counters.",
                flush=True,
            )
        print(
            "[orchestrator] analysis-only failure finalized without patch artifacts.",
            flush=True,
        )
        return evidence_path

    # Persist per-round build verification diagnostics.
    if build_verify_log:
        bv_path = output_dir / "compile_check.json"
        bv_path.write_text(
            json.dumps(build_verify_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[orchestrator] Compile check log saved -> {bv_path}", flush=True)

    # Collect git diff \u2014 pass planned files so newly-created files are
    # promoted from untracked to intent-to-add and surface in the diff.
    # Use the UNION of every file any plan round intended to touch so a repatch
    # (which overwrote memory.patch_plan with the fixup plan) does not drop a
    # file the first round created.  The narrower coverage check below uses the
    # final plan only, to avoid false downgrades from first-round files that
    # turned out unnecessary.
    final_verification_plan = _merge_patch_plans(aggregate_patch_plan, wm.patch_plan)
    final_plan_files: list[str] = []
    if final_verification_plan is not None:
        final_plan_files = [
            edit.filepath for edit in final_verification_plan.edits
            if not edit.reference_only
        ]
    diff_planned_files = sorted(all_planned_files | set(final_plan_files))
    diff_text = _collect_git_diff(repo_dir, planned_files=diff_planned_files)
    if diff_text.startswith("\ufeff"):
        diff_text = diff_text.lstrip("\ufeff")

    final_artifact_result = verify_patch_artifacts(
        repo_dir,
        final_verification_plan,
        diff_text,
    )
    artifact_verify_log.append({"round": "final", **final_artifact_result.to_log()})
    patch_outcome, closure_approved = _reconcile_final_patch_outcome(
        patch_outcome,
        closure_approved,
        artifact_ok=final_artifact_result.ok,
        artifact_empty_patch=final_artifact_result.empty_patch,
    )
    if artifact_verify_log:
        av_path = output_dir / "artifact_verification.json"
        av_path.write_text(
            json.dumps(artifact_verify_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[orchestrator] Artifact verification log saved -> {av_path}", flush=True)

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

    # Remove checkpoint now that the pipeline has reached a terminal state and
    # all output files are written.  A fresh restart is always preferred over
    # resuming from a completed (or failed) run.
    _delete_checkpoint(output_dir)

    return evidence_path


def run_orchestrator(
    issue_id: str,
    repo_dir: str | Path,
    artifact_text: str,
    output_dir: str | Path,
    problem_statement: str = "",
    *,
    stop_after_closure: bool = False,
) -> Path:
    """Synchronous entry-point. Returns the path to evidence.json."""
    return asyncio.run(
        run_pipeline(
            issue_id=issue_id,
            repo_dir=repo_dir,
            artifact_text=artifact_text,
            output_dir=output_dir,
            problem_statement=problem_statement,
            stop_after_closure=stop_after_closure,
        )
    )
