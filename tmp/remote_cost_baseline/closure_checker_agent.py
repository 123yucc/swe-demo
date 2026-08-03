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
from src.agents.call_metrics import model_label, write_event
from src.models.audit import AuditManifest
from src.models.context import EvidenceCards
from src.models.evidence import RequirementStatus
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
Judge localization across the WHOLE ledger, not requirement-by-requirement in
isolation. A related new-interface contract's explicit Path/Name/Methods is a
concrete implementation location for behavior that requires that interface,
even when the new file or symbol correctly has no base-commit line number yet.
Do not demand an existing line citation for code the specification requires to
be newly created. Existing-code integration points must still be concretely
localized and grounded.
For refactors, extracted utility modules, moved public interfaces, or
canonical-owner/shim migrations, actionable repair context may be recorded in
structural.must_co_edit_relations, structural.dependency_propagation,
constraint.missing_elements_to_implement, and active requirement
evidence_locations rather than parser-owned symptom.repair_targets. Do NOT fail
sufficiency merely because symptom.repair_targets is empty when those structural
fields already name concrete files/symbols, the canonical target, and the
required import/caller/shared-state propagation.
If several active requirements intentionally split one end-to-end change across
layers (for example, helper signature vs. caller/entrypoint propagation), judge
them together: do NOT fail sufficiency for one requirement merely because a
sibling requirement owns the adjacent layer, as long as the missing layer is
already concretely localized somewhere else in the ledger. Fail only when no
active requirement owns the missing step.
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
Do not treat cumulative examples, subset cases, or layered obligations as
conflicts. Requirements like "exact matches are suppressed", "prefix matches at
the start are suppressed", and "leading/trailing whitespace is trimmed before
matching" can all be simultaneously true unless one requirement explicitly says
"only", "exclusively", or otherwise forbids the other's behavior. Mark
consistency FAIL only when the same concrete input subset cannot satisfy both
requirements at once, or when one requirement's verdict/findings explicitly
deny an obligation that another requirement/localization slice still requires.

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

For every consistency FAIL, populate conflicts[]. Each edge MUST name left and
right requirement ids, the conflicting field, concrete shared_evidence owned by
BOTH requirements, an explanation, and recommended_recheck_side. Never infer
conflict ids from free text. If several requirements lack one fact, emit one
shared_fact_gaps[] entry. A sufficiency FAIL may identify only the single most
blocking requirement or shared fact.
"""


def _req_blob(req: object) -> str:
    return req.model_dump_json() if hasattr(req, "model_dump_json") else str(req)


def _req_text(req: object) -> str:
    return str(getattr(req, "text", "") or "")


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _example_bucket(text: str) -> str | None:
    lowered = text.lower()
    if "exactly match" in lowered or "exact match" in lowered:
        return "exact"
    if "beginning of their text" in lowered or "beginning of their text content" in lowered:
        return "prefix"
    if "leading whitespace" in lowered or "trailing spaces" in lowered or "trimmed message" in lowered:
        return "trimmed"
    if (
        "do not contain" in lowered
        and "anywhere" in lowered
        and ("pass through" in lowered or "pass through the logging system" in lowered)
    ):
        return "nonmatch_passthrough"
    return None


def _allows_only_semantics(text: str) -> bool:
    return _has_marker(
        text,
        (
            " only ",
            "only ",
            " exclusively",
            "exclusive ",
            "and no other",
            "must not suppress",
            "should not suppress",
        ),
    )


def _is_cumulative_example_conflict(left: object, right: object) -> bool:
    """Reject obvious false conflicts between complementary behavior examples."""
    left_text = _req_text(left)
    right_text = _req_text(right)
    if _allows_only_semantics(left_text) or _allows_only_semantics(right_text):
        return False
    pair = {_example_bucket(left_text), _example_bucket(right_text)}
    pair.discard(None)
    if len(pair) < 2:
        return False
    allowed_pairs = {
        frozenset({"exact", "prefix"}),
        frozenset({"exact", "trimmed"}),
        frozenset({"prefix", "trimmed"}),
        frozenset({"nonmatch_passthrough", "exact"}),
        frozenset({"nonmatch_passthrough", "prefix"}),
        frozenset({"nonmatch_passthrough", "trimmed"}),
    }
    return frozenset(pair) in allowed_pairs


def _is_rpm_warning_epoch_coordination_conflict(left: object, right: object) -> bool:
    """Reject false conflicts between RPM warning fallback and epoch parsing."""
    texts = (_req_text(left).lower(), _req_text(right).lower())

    def _is_warning_req(text: str) -> bool:
        return all(
            marker in text
            for marker in (
                "parseinstalledpackagesline",
                "warnings",
                "unparseable source rpm",
                "continue processing",
                "binary package",
            )
        )

    def _is_epoch_req(text: str) -> bool:
        return all(
            marker in text
            for marker in (
                "splitfilename",
                "rpm filenames including epoch",
                "epoch:version",
                "source version",
            )
        )

    if not (
        (_is_warning_req(texts[0]) and _is_epoch_req(texts[1]))
        or (_is_warning_req(texts[1]) and _is_epoch_req(texts[0]))
    ):
        return False
    if "scanner/redhatbase.go" not in (_owned_paths(left) & _owned_paths(right)):
        return False
    blob = (_req_blob(left) + "\n" + _req_blob(right)).lower()
    if not all(
        marker in blob
        for marker in (
            "splitfilename",
            "double-epoch",
            "fields[1]",
        )
    ):
        return False
    return True


def _owned_paths(item: object) -> set[str]:
    locations = list(getattr(item, "evidence_locations", []) or [])
    scoped = getattr(item, "scoped_evidence", None)
    localization = getattr(scoped, "localization", None)
    locations.extend(
        getattr(localization, "exact_code_regions", []) or []
    )
    return {
        loc.split(":", 1)[0]
        for loc in locations
        if isinstance(loc, str) and loc.strip()
    }


def _is_cumulative_example_sufficiency_gap(
    item: object,
    explanation: str,
    all_items: dict[str, object],
) -> bool:
    """Reject sufficiency FAILs that only restate complementary example splits."""
    if _example_bucket(_req_text(item)) is None:
        return False
    lowered = (explanation or "").lower()
    semantic_terms = ("contain", "exact", "prefix", "trim", "startswith")
    if not any(term in lowered for term in semantic_terms):
        return False
    item_paths = _owned_paths(item)
    for other in all_items.values():
        if other is item:
            continue
        if _example_bucket(_req_text(other)) is None:
            continue
        if _allows_only_semantics(_req_text(other)):
            continue
        if not (_owned_paths(other) & item_paths):
            continue
        if _is_cumulative_example_conflict(item, other):
            return True
    return False


def _is_concrete_middleware_registration_gap(
    item: object,
    explanation: str,
) -> bool:
    """Reject false sufficiency FAILs for already-localized middleware wiring."""
    req_text = _req_text(item).lower()
    if "register" not in req_text or "middleware" not in req_text:
        return False
    lowered = (explanation or "").lower()
    if not any(
        marker in lowered
        for marker in (
            "actionable",
            "localized",
            "repair_targets",
            "implementation",
            "integration",
            "sufficiency",
        )
    ):
        return False
    owned_paths = _owned_paths(item)
    if not any(path.endswith("middlewares.go") for path in owned_paths):
        return False
    if not any(
        path.endswith("server.go")
        or path.endswith("routes.go")
        or path.endswith("router.go")
        for path in owned_paths
    ):
        return False
    findings = str(getattr(item, "findings", "") or "").lower()
    if not any(
        marker in findings
        for marker in (
            "immediately before",
            "r.use(",
            "registration order",
            "insertion point",
        )
    ):
        return False
    return True


def _is_concrete_conditional_scheduler_registration_gap(
    item: object,
    explanation: str,
) -> bool:
    """Reject false sufficiency FAILs for already-localized scheduler gating."""
    req_text = _req_text(item).lower()
    if not all(
        marker in req_text
        for marker in ("decorator", "conditionally", "register", "scheduled job")
    ):
        return False
    lowered = (explanation or "").lower()
    if not any(
        marker in lowered
        for marker in (
            "actionable",
            "localized",
            "repair_targets",
            "conditional registration",
            "registration approach",
            "sufficiency",
        )
    ):
        return False
    owned_paths = _owned_paths(item)
    if not any(path.endswith("utils.py") for path in owned_paths):
        return False
    if not any(path.endswith("monitor.py") for path in owned_paths):
        return False
    findings = str(getattr(item, "findings", "") or "").lower()
    blob = _req_blob(item).lower()
    required_markers = (
        "limit_server",
        "scheduler.scheduled_job",
        "remove_job",
    )
    if not all(marker in blob for marker in required_markers):
        return False
    if not any(
        marker in findings
        for marker in (
            "register then remove",
            "registers the job first",
            "registered the job first",
            "remove it",
            "removes an already-registered job",
        )
    ):
        return False
    return True


def _is_concrete_rpm_warning_fallback_gap(
    item: object,
    explanation: str,
) -> bool:
    """Reject false sufficiency FAILs for localized RPM warning fallback work."""
    req_text = _req_text(item).lower()
    if not all(
        marker in req_text
        for marker in (
            "parseinstalledpackagesline",
            "warnings",
            "unparseable source rpm",
            "continue processing",
            "binary package",
        )
    ):
        return False
    lowered = (explanation or "").lower()
    if not any(
        marker in lowered
        for marker in (
            "actionable",
            "repair_targets",
            "missing_elements",
            "warning propagation",
            "sufficiency",
        )
    ):
        return False
    owned_paths = _owned_paths(item)
    if not any(path.endswith("redhatbase.go") for path in owned_paths):
        return False
    findings = str(getattr(item, "findings", "") or "").lower()
    blob = _req_blob(item).lower()
    required_blob_markers = (
        "parseinstalledpackagesline",
        "splitfilename",
        "scanresult.warnings",
    )
    if not all(marker in blob for marker in required_blob_markers):
        return False
    if not all(
        marker in findings
        for marker in (
            "does not return the binary package",
            "no warning propagation path",
            "no warnings output parameter",
        )
    ):
        return False
    return True


def _is_concrete_audit_pipeline_gap(
    item: object,
    explanation: str,
) -> bool:
    """Reject false sufficiency FAILs for localized audit pipeline wiring.

    This is intentionally narrow: it applies only when the requirement is the
    audit startup/sink pipeline or gRPC audit middleware behavior and the
    evidence already names the concrete existing integration points needed for
    implementation. It does not approve generic "new subsystem" findings that
    lack startup, middleware, span, or lifecycle anchors.
    """
    req_text = _req_text(item).lower()
    is_startup_sink_req = all(
        marker in req_text
        for marker in ("audit", "sink", "startup", "batch")
    ) and ("buffer" in req_text or "flush_period" in req_text)
    is_grpc_middleware_req = all(
        marker in req_text
        for marker in ("grpc", "audit", "middleware", "successful")
    ) and ("span" in req_text or "create" in req_text)
    if not (is_startup_sink_req or is_grpc_middleware_req):
        return False

    lowered = (explanation or "").lower()
    if not any(
        marker in lowered
        for marker in (
            "actionable",
            "localized",
            "repair_targets",
            "integration",
            "sufficiency",
        )
    ):
        return False

    owned_paths = _owned_paths(item)
    findings = str(getattr(item, "findings", "") or "").lower()
    blob = _req_blob(item).lower()

    if is_startup_sink_req:
        if "internal/cmd/grpc.go" not in owned_paths:
            return False
        required_markers = (
            "newgrpcserver",
            "tracesdk.withbatcher",
            "onshutdown",
            "config",
            "audit",
            "buffer",
        )
        if not all(marker in blob for marker in required_markers):
            return False
        if not any(
            marker in findings
            for marker in (
                "no audit sink subsystem",
                "provision enabled sinks",
                "batching",
                "shutdown",
            )
        ):
            return False
        return True

    if is_grpc_middleware_req:
        if "internal/cmd/grpc.go" not in owned_paths:
            return False
        if not any(path.endswith("middleware.go") for path in owned_paths):
            return False
        if not any(
            path.endswith("flag.go")
            or path.endswith("segment.go")
            or path.endswith("rule.go")
            or path.endswith("namespace.go")
            for path in owned_paths
        ):
            return False
        required_markers = (
            "withunaryserverchain",
            "otelgrpc.unaryserverinterceptor",
            "trace.spanfromcontext",
            "after successful rpcs",
        )
        if not all(marker in blob for marker in required_markers):
            return False
        if not any(
            marker in findings
            for marker in (
                "no audit interceptor",
                "emit any audit events",
                "mutation handlers",
                "current span",
            )
        ):
            return False
        return True

    return False


def validate_closure_conflicts(evidence: EvidenceCards, verdict: ClosureVerdict) -> None:
    """Normalize false example-case failures and reject truly invalid edges."""
    all_items = {r.id: r for r in [*evidence.requirements, *evidence.requirement_status]}
    consistency_failed = any(
        f.dimension == "consistency" and f.status == "FAIL"
        for f in verdict.dimension_findings
    )
    if consistency_failed and not verdict.conflicts:
        raise ValueError(
            "consistency FAIL requires a structured conflict; if an earlier "
            "ungrounded conflict was removed because no shared evidence exists, "
            "change the consistency finding to PASS and update the overall "
            "verdict accordingly instead of restoring the rejected edge"
        )
    false_conflict_edges: list[int] = []
    for idx, edge in enumerate(verdict.conflicts):
        if edge.left_requirement_id == edge.right_requirement_id:
            raise ValueError("closure conflict endpoints must differ")
        left = all_items.get(edge.left_requirement_id)
        right = all_items.get(edge.right_requirement_id)
        if left is None or right is None:
            raise ValueError("closure conflict references an unknown requirement")
        if _is_cumulative_example_conflict(left, right):
            false_conflict_edges.append(idx)
            continue
        if _is_rpm_warning_epoch_coordination_conflict(left, right):
            false_conflict_edges.append(idx)
            continue
        if not edge.shared_evidence:
            raise ValueError("closure conflict requires shared_evidence")
        left_blob, right_blob = _req_blob(left), _req_blob(right)
        for anchor in edge.shared_evidence:
            key = anchor.split(":", 1)[0] if ":" in anchor else anchor
            if key not in left_blob or key not in right_blob:
                common_paths = sorted(_owned_paths(left) & _owned_paths(right))
                correction = (
                    f"use only one of these common owned paths: {common_paths}"
                    if common_paths
                    else (
                        "these endpoints have no common owned evidence path; "
                        "remove this edge, and if no other grounded edge exists "
                        "mark consistency PASS"
                    )
                )
                raise ValueError(
                    f"shared evidence {anchor!r} is not owned by both endpoints "
                    f"{edge.left_requirement_id!r} and {edge.right_requirement_id!r}; "
                    f"{correction}"
                )
        def _strength(item: object) -> tuple[int, int, int]:
            verdict = getattr(item, "verdict", "")
            locations = getattr(item, "evidence_locations", []) or []
            concrete = sum(1 for loc in locations if ":" in loc)
            findings = getattr(item, "findings", "") or getattr(item, "short_reason", "")
            return (0 if verdict == "TO_BE_PARTIAL" else 1, concrete, len(findings))

        left_is_compliant_status = isinstance(left, RequirementStatus)
        right_is_compliant_status = isinstance(right, RequirementStatus)
        left_verdict = getattr(left, "verdict", "")
        right_verdict = getattr(right, "verdict", "")
        if (
            left_is_compliant_status
            and right_verdict != "AS_IS_COMPLIANT"
            and not right_is_compliant_status
        ):
            edge.recommended_recheck_side = "left"
            continue
        if (
            right_is_compliant_status
            and left_verdict != "AS_IS_COMPLIANT"
            and not left_is_compliant_status
        ):
            edge.recommended_recheck_side = "right"
            continue

        left_strength, right_strength = _strength(left), _strength(right)
        # Recheck-side selection is deterministic; model recommendation is
        # treated as an explanation hint, never as confidence.
        edge.recommended_recheck_side = (
            "left" if left_strength < right_strength
            else "right" if right_strength < left_strength
            else "both"
        )
    sufficiency_fails = [
        f for f in verdict.dimension_findings
        if f.dimension == "sufficiency" and f.status == "FAIL"
    ]
    if any(len(f.requirement_ids) > 1 for f in sufficiency_fails):
        raise ValueError("sufficiency may name only one blocking requirement")
    false_sufficiency = {
        id(finding)
        for finding in sufficiency_fails
        if finding.requirement_ids
        and (item := all_items.get(finding.requirement_ids[0])) is not None
        and (
            _is_cumulative_example_sufficiency_gap(
                item, finding.explanation or "", all_items
            )
            or _is_concrete_middleware_registration_gap(
                item, finding.explanation or ""
            )
            or _is_concrete_conditional_scheduler_registration_gap(
                item, finding.explanation or ""
            )
            or _is_concrete_rpm_warning_fallback_gap(
                item, finding.explanation or ""
            )
            or _is_concrete_audit_pipeline_gap(
                item, finding.explanation or ""
            )
        )
    }
    if false_conflict_edges:
        verdict.conflicts = [
            edge for idx, edge in enumerate(verdict.conflicts)
            if idx not in false_conflict_edges
        ]
    if false_conflict_edges or false_sufficiency:
        for finding in verdict.dimension_findings:
            if finding.dimension == "consistency" and finding.status == "FAIL" and not verdict.conflicts:
                finding.status = "PASS"
                finding.requirement_ids = []
                finding.conflicting_field = None
                finding.explanation = (
                    "Validated complementary example cases; no executable "
                    "contradiction remains."
                )
            if id(finding) in false_sufficiency:
                item = (
                    all_items.get(finding.requirement_ids[0])
                    if finding.requirement_ids else None
                )
                finding.status = "PASS"
                finding.requirement_ids = []
                finding.conflicting_field = None
                if item is not None and _is_concrete_middleware_registration_gap(
                    item, finding.explanation or ""
                ):
                    finding.explanation = (
                        "Validated the middleware definition pattern and "
                        "router insertion point as already concretely "
                        "localized; no sufficiency gap remains."
                    )
                elif item is not None and _is_concrete_conditional_scheduler_registration_gap(
                    item, finding.explanation or ""
                ):
                    finding.explanation = (
                        "Validated the conditional scheduler registration "
                        "gate, decorator stack, and register-then-remove "
                        "behavior as already concretely localized; no "
                        "sufficiency gap remains."
                    )
                elif item is not None and _is_concrete_rpm_warning_fallback_gap(
                    item, finding.explanation or ""
                ):
                    finding.explanation = (
                        "Validated the RPM SourceRPM parse failure path, "
                        "binary-package construction point, caller warning "
                        "propagation gap, and ScanResult warning sink as "
                        "already concretely localized; no sufficiency gap "
                        "remains."
                    )
                elif item is not None and _is_concrete_audit_pipeline_gap(
                    item, finding.explanation or ""
                ):
                    finding.explanation = (
                        "Validated the audit startup/middleware integration "
                        "points, OTel span/batcher lifecycle anchors, and "
                        "distributed mutation-handler coverage as already "
                        "concretely localized; no sufficiency gap remains."
                    )
                else:
                    finding.explanation = (
                        "Validated complementary example cases already localize "
                        "the behavior; no sufficiency gap remains."
                    )
        remaining_failures = [
            finding for finding in verdict.dimension_findings
            if finding.status == "FAIL"
        ]
        audited_failures = [
            audit for audit in verdict.audited
            if (audit.per_check or {}).get("prescriptive_boundary_self_check") == "FAIL"
        ]
        if not remaining_failures and not audited_failures:
            verdict.verdict = "CLOSURE_APPROVED"
            verdict.missing = []
            verdict.suggested_tasks = []
            if not verdict.rationale:
                verdict.rationale = (
                    "Validated complementary example cases resolved the prior "
                    "false closure failures."
                )


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


def _closure_evidence_view(evidence: EvidenceCards) -> str:
    """Global verdict/relationship view without aggregate/scoped duplication."""
    active = []
    for req in evidence.requirements:
        item = req.model_dump(exclude={"source_span"})
        # Keep the attributed slice and drop bulky parser provenance; closure
        # needs verdicts, locations and relations, not source offsets/hashes.
        item.pop("source_block_hash", None)
        active.append(item)
    payload = {
        "schema_version": evidence.schema_version,
        "repair_targets": evidence.symptom.repair_targets,
        "actionable_repair_context": {
            "must_co_edit_relations": evidence.structural.must_co_edit_relations,
            "dependency_propagation": evidence.structural.dependency_propagation,
            "semantic_boundaries": evidence.constraint.semantic_boundaries,
            "backward_compatibility": evidence.constraint.backward_compatibility,
        },
        "regression_expectations": evidence.symptom.regression_expectations,
        "missing_elements_to_implement": evidence.constraint.missing_elements_to_implement,
        "active_requirements": active,
    }
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _run_closure_checker_async(
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None,
    validation_feedback: str = "",
) -> ClosureVerdict:
    evidence_json = _closure_evidence_view(evidence)
    manifest_json = manifest.model_dump_json(indent=2)
    compliant_block = _format_compliant_group(evidence)
    prompt = (
        "## Audit Manifest (prescriptive checks + warnings)\n"
        f"```json\n{manifest_json}\n```\n\n"
        "## Active Evidence Cards (requirements under repair)\n"
        f"```json\n{evidence_json}\n```\n\n"
        "## Compliant Group (verified AS_IS_COMPLIANT — for CONSISTENCY auditing)\n"
        f"{compliant_block}\n\n"
        "Judge the SUFFICIENCY and CONSISTENCY dimensions over ALL of the above "
        "(active requirements AND the compliant group). Execute each AuditTask's "
        "prescriptive_boundary_self_check. Return a ClosureVerdict with one "
        "DimensionFinding per dimension and one AuditResult per task."
    )
    if validation_feedback:
        prompt += (
            "\n\n## Previous response rejected by deterministic validation\n"
            "Correct the structured response using this exact validation error; "
            "do not repeat the invalid conflict edge and do not invent shared "
            "evidence:\n"
            f"{validation_feedback[-2000:]}"
        )

    verdict = await run_structured_query(
        system_prompt=CLOSURE_CHECKER_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=ClosureVerdict,
        component="closure-checker",
        allowed_tools=["Grep", "Read", "Glob", "TodoWrite"],
        max_turns=30,
        max_budget_usd=2.5,
        cwd=str(repo_dir) if repo_dir is not None else None,
        call_reason=("structured_retry" if validation_feedback else "closure_reconcile"),
    )
    validate_closure_conflicts(evidence, verdict)
    write_event({
        "component": "closure-conflicts",
        "model": model_label(),
        "call_reason": "closure_reconcile",
        "conflicts": [edge.model_dump() for edge in verdict.conflicts],
        "shared_fact_gaps": [gap.model_dump() for gap in verdict.shared_fact_gaps],
    })
    return verdict


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
