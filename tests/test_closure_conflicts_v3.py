import asyncio

import pytest

import src.agents.closure_checker_agent as closure_agent
from src.agents.closure_checker_agent import _closure_evidence_view, validate_closure_conflicts
from src.models.audit import AuditManifest, DimensionFinding
from src.models.context import EvidenceCards
from src.models.evidence import ConstraintCard, LocalizationCard, RequirementItem, RequirementStatus, StructuralCard, SymptomCard
from src.models.verdict import ClosureConflict, ClosureVerdict, SharedFactGap
from src.orchestrator.engine import (
    _build_per_req_audit_feedback,
    _derive_rework_specs,
    _eligible_closure_rework_ids,
    _semantic_field_feedback,
)


def _evidence():
    return EvidenceCards(
        symptom=SymptomCard(), constraint=ConstraintCard(),
        localization=LocalizationCard(), structural=StructuralCard(),
        requirements=[
            RequirementItem(id="req-001", text="a", origin="requirements", verdict="AS_IS_VIOLATED", evidence_locations=["src/a.py:10"]),
            RequirementItem(id="req-002", text="b", origin="requirements", verdict="TO_BE_PARTIAL", evidence_locations=["src/a.py:20"]),
        ],
    )


def test_closure_evidence_view_includes_structural_actionable_context():
    evidence = EvidenceCards(
        symptom=SymptomCard(repair_targets=[]),
        constraint=ConstraintCard(
            missing_elements_to_implement=["Type: New File\nPath: src/new_owner.py"],
            semantic_boundaries=["Preserve shared state initialization."],
            backward_compatibility=["Keep old import surface as a delegating shim."],
        ),
        localization=LocalizationCard(),
        structural=StructuralCard(
            must_co_edit_relations=[
                "Move helper implementation into src/new_owner.py and re-export from src/old_owner.py."
            ],
            dependency_propagation=[
                "src/caller.py imports must follow the selected shim-vs-rewrite strategy."
            ],
        ),
        requirements=[
            RequirementItem(
                id="req-001",
                text="extract helper into explicit new owner",
                origin="requirements",
                verdict="AS_IS_VIOLATED",
                evidence_locations=["src/old_owner.py:10-30", "src/caller.py:5"],
            )
        ],
    )

    view = _closure_evidence_view(evidence)

    assert '"repair_targets": []' in view
    assert "actionable_repair_context" in view
    assert "must_co_edit_relations" in view
    assert "src/new_owner.py" in view
    assert "src/caller.py" in view


def test_conflict_requires_real_shared_evidence():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[DimensionFinding(dimension="consistency", status="FAIL", requirement_ids=["req-001", "req-002"], conflicting_field="<cross-req>")],
        conflicts=[ClosureConflict(left_requirement_id="req-001", right_requirement_id="req-002", conflicting_field="verdict", shared_evidence=["src/a.py"], explanation="opposite claims", recommended_recheck_side="right")],
    )
    validate_closure_conflicts(_evidence(), verdict)


def test_conflict_prefers_rechecking_compliant_status_side():
    evidence = EvidenceCards(
        symptom=SymptomCard(), constraint=ConstraintCard(),
        localization=LocalizationCard(), structural=StructuralCard(),
        requirements=[
            RequirementItem(
                id="req-013",
                text="Rename the queue API.",
                origin="requirements",
                verdict="AS_IS_VIOLATED",
                evidence_locations=["server/events/diode.go:13"],
                findings="The base code still exposes set and needs a rename to put.",
            ),
        ],
        requirement_status=[
            RequirementStatus(
                id="req-015",
                text="Use the updated queue API when sending the startup event.",
                origin="requirements",
                short_reason="The startup event is already queued via c.diode.set(...).",
                evidence_locations=["server/events/diode.go:13", "server/events/sse.go:180"],
            ),
        ],
    )
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-013", "req-015"],
                conflicting_field="<cross-req>",
            )
        ],
        conflicts=[
            ClosureConflict(
                left_requirement_id="req-015",
                right_requirement_id="req-013",
                conflicting_field="<cross-req>",
                shared_evidence=["server/events/diode.go"],
                explanation="The compliant status cites the old set API while the active requirement says that API is not updated yet.",
                recommended_recheck_side="right",
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)

    assert verdict.conflicts[0].recommended_recheck_side == "left"
    specs = _derive_rework_specs(verdict)
    assert sorted(specs) == ["req-015"]


def test_flat_consistency_failure_is_schema_failure():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[DimensionFinding(dimension="consistency", status="FAIL", requirement_ids=["req-001", "req-002"], conflicting_field="<cross-req>")],
    )
    with pytest.raises(ValueError, match="change the consistency finding to PASS"):
        validate_closure_conflicts(_evidence(), verdict)


def test_invalid_conflict_reports_endpoints_and_valid_common_paths():
    evidence = _evidence()
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-001", "req-002"],
                conflicting_field="<cross-req>",
            )
        ],
        conflicts=[
            ClosureConflict(
                left_requirement_id="req-001",
                right_requirement_id="req-002",
                conflicting_field="verdict",
                shared_evidence=["src/not-owned.py:1"],
                explanation="claims conflict",
                recommended_recheck_side="both",
            )
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_closure_conflicts(evidence, verdict)
    message = str(exc_info.value)
    assert "req-001" in message and "req-002" in message
    assert "src/a.py" in message


def test_closure_semantic_retry_includes_validation_feedback(monkeypatch):
    captured = {}

    async def fake_query(**kwargs):
        captured.update(kwargs)
        return ClosureVerdict(
            verdict="CLOSURE_APPROVED",
            rationale="corrected",
        )

    monkeypatch.setattr(closure_agent, "run_structured_query", fake_query)
    verdict = asyncio.run(
        closure_agent._run_closure_checker_async(
            _evidence(),
            AuditManifest(),
            validation_feedback="ValueError: invalid shared evidence edge",
        )
    )

    assert verdict.verdict == "CLOSURE_APPROVED"
    assert "invalid shared evidence edge" in captured["user_prompt"]
    assert captured["call_reason"] == "structured_retry"


def test_shared_fact_gap_is_included_in_rework_feedback():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        rationale="biggest gap is adoption map",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-001"],
                conflicting_field="repair_targets",
                explanation="integration map is missing",
            )
        ],
        shared_fact_gaps=[
            SharedFactGap(
                fact="Complete import/call-site map for moved Solr utilities.",
                requirement_ids=["req-001", "req-005"],
                suggested_anchor="Grep for openlibrary.solr.update_work imports.",
            )
        ],
    )

    feedback = _build_per_req_audit_feedback(verdict, ["req-001"])

    assert "Complete import/call-site map" in feedback["req-001"]
    assert "Grep for openlibrary.solr.update_work imports" in feedback["req-001"]
    specs = _derive_rework_specs(verdict)
    assert "deep-search cannot write parser-owned symptom.repair_targets" in (
        specs["req-001"].feedback
    )
    assert "must_co_edit_relations" in specs["req-001"].feedback
    assert "dependency_propagation" in specs["req-001"].feedback


def test_closure_rework_cap_is_per_requirement():
    eligible, frozen, capped = _eligible_closure_rework_ids(
        ["req-001", "req-008", "req-009"],
        {"req-009"},
        {"req-001": 3},
        3,
    )

    assert eligible == ["req-008"]
    assert frozen == ["req-009"]
    assert capped == ["req-001"]


def test_closure_prompt_treats_new_interface_path_as_concrete_location():
    assert "new-interface contract's explicit Path/Name/Methods" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )
    assert "Do not demand an existing line citation" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )


def test_closure_prompt_keeps_split_layer_requirements_global():
    assert "split one end-to-end change across" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )
    assert "sibling requirement owns the adjacent layer" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )


def test_closure_prompt_avoids_false_conflicts_for_cumulative_examples():
    assert "Do not treat cumulative examples, subset cases" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )
    assert "exact matches are suppressed" in (
        closure_agent.CLOSURE_CHECKER_SYSTEM_PROMPT
    )


def test_cumulative_example_conflict_is_normalized():
    evidence = EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=[
            RequirementItem(
                id="req-001",
                text="Log entries that exactly match the provided filter string should be completely suppressed.",
                origin="requirements",
                verdict="AS_IS_VIOLATED",
                evidence_locations=["src/filter.py:10"],
            ),
            RequirementItem(
                id="req-002",
                text="Messages containing the filter pattern at the beginning of their text content should be blocked from logging.",
                origin="requirements",
                verdict="AS_IS_COMPLIANT",
                evidence_locations=["src/filter.py:10"],
            ),
        ],
    )
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-001", "req-002"],
                conflicting_field="verdict",
            )
        ],
        conflicts=[
            ClosureConflict(
                left_requirement_id="req-001",
                right_requirement_id="req-002",
                conflicting_field="verdict",
                shared_evidence=["src/filter.py:10"],
                explanation="exact-only versus prefix behavior",
                recommended_recheck_side="both",
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)
    assert verdict.verdict == "CLOSURE_APPROVED"
    assert verdict.conflicts == []
    consistency = next(f for f in verdict.dimension_findings if f.dimension == "consistency")
    assert consistency.status == "PASS"


def test_cumulative_example_sufficiency_gap_is_normalized():
    evidence = EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=[
            RequirementItem(
                id="req-001",
                text="Warning messages that do not contain the specified filter pattern anywhere in their text should pass through the logging system unmodified.",
                origin="requirements",
                verdict="AS_IS_VIOLATED",
                evidence_locations=["src/filter.py:10"],
            ),
            RequirementItem(
                id="req-002",
                text="Messages containing the filter pattern at the beginning of their text content should be blocked from logging.",
                origin="requirements",
                verdict="AS_IS_COMPLIANT",
                evidence_locations=["src/filter.py:10"],
            ),
            RequirementItem(
                id="req-003",
                text="Filter pattern matching should properly handle warning messages that include leading whitespace characters or trailing spaces by applying the pattern comparison logic against the trimmed message content.",
                origin="requirements",
                verdict="AS_IS_COMPLIANT",
                evidence_locations=["src/filter.py:10"],
            ),
        ],
    )
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-001"],
                conflicting_field="findings",
                explanation=(
                    "The evidence does not reconcile whether matching should be "
                    "containment-based, exact-match-based, or prefix-based for "
                    "the same suppression path."
                ),
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)
    assert verdict.verdict == "CLOSURE_APPROVED"
    sufficiency = next(f for f in verdict.dimension_findings if f.dimension == "sufficiency")
    assert sufficiency.status == "PASS"


def test_repair_target_feedback_requires_single_refactor_strategy():
    feedback = _semantic_field_feedback("repair_targets")

    assert "choose ONE concrete steady-state strategy" in feedback
    assert "Do not leave an either/or plan" in feedback
    assert "canonical owner" in feedback
    assert "delegated, or re-exported" in feedback
    assert "exact path/name" in feedback
    assert "tests as compatibility evidence" in feedback


def test_middleware_registration_sufficiency_gap_is_normalized():
    evidence = EvidenceCards.model_validate({
        "symptom": SymptomCard().model_dump(),
        "constraint": ConstraintCard().model_dump(),
        "localization": LocalizationCard().model_dump(),
        "structural": StructuralCard().model_dump(),
        "requirements": [
            {
                "id": "req-017",
                "text": "In the router setup, register the new client-unique-id middleware before the logger and request logger middlewares.",
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "server/server.go:49-68",
                    "server/middlewares.go:15-57",
                ],
                "findings": (
                    "Verified the global chi router middleware registration order. "
                    "The correct insertion point is immediately before "
                    "r.Use(injectLogger) / r.Use(requestLogger), and "
                    "server/middlewares.go shows the existing middleware "
                    "definition pattern."
                ),
                "scoped_evidence": {
                    "localization": {
                        "exact_code_regions": [
                            "server/server.go:49-68",
                            "server/middlewares.go:15-57",
                        ]
                    }
                },
            }
        ],
    })
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-017"],
                conflicting_field="repair_targets",
                explanation=(
                    "repair_targets are not yet actionable/localized for the "
                    "new client-unique-id middleware implementation/integration"
                ),
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)
    assert verdict.verdict == "CLOSURE_APPROVED"
    sufficiency = next(f for f in verdict.dimension_findings if f.dimension == "sufficiency")
    assert sufficiency.status == "PASS"
    assert "middleware definition pattern" in sufficiency.explanation


def test_conditional_scheduler_registration_sufficiency_gap_is_normalized():
    evidence = EvidenceCards.model_validate({
        "symptom": SymptomCard().model_dump(),
        "constraint": ConstraintCard().model_dump(),
        "localization": LocalizationCard().model_dump(),
        "structural": StructuralCard().model_dump(),
        "requirements": [
            {
                "id": "req-002",
                "text": (
                    "The decorator limit_server(allowed_hosts, scheduler) must "
                    "conditionally register a scheduled job based on the current "
                    "host name from the environment; when the host matches any "
                    "allowed pattern the job is registered, otherwise it is not "
                    "registered."
                ),
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "scripts/monitoring/utils.py:102-119",
                    "scripts/monitoring/monitor.py:19-28",
                    "scripts/monitoring/tests/test_utils_py.py:26-48",
                ],
                "findings": (
                    "limit_server reads HOSTNAME and checks allowed hosts. In "
                    "actual usage @scheduler.scheduled_job(...) registers the "
                    "job first, and only afterwards @limit_server(...) calls "
                    "scheduler.remove_job(func.__name__) on disallowed hosts. "
                    "This is register then remove, not true conditional "
                    "registration."
                ),
                "scoped_evidence": {
                    "localization": {
                        "exact_code_regions": [
                            "scripts/monitoring/utils.py:102-119",
                            "scripts/monitoring/monitor.py:19-28",
                        ],
                        "call_chain_context": [
                            "apply @scheduler.scheduled_job(...) -> registered "
                            "job first -> apply @limit_server(...) -> "
                            "scheduler.remove_job(func.__name__)"
                        ],
                    }
                },
            }
        ],
    })
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-002"],
                conflicting_field="repair_targets",
                explanation="req-002 sufficiency: conditional registration approach not actionable/localized",
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)
    assert verdict.verdict == "CLOSURE_APPROVED"
    sufficiency = next(f for f in verdict.dimension_findings if f.dimension == "sufficiency")
    assert sufficiency.status == "PASS"
    assert "conditional scheduler registration" in sufficiency.explanation


def test_rpm_warning_fallback_sufficiency_gap_is_normalized():
    evidence = EvidenceCards.model_validate({
        "symptom": SymptomCard().model_dump(),
        "constraint": ConstraintCard().model_dump(),
        "localization": LocalizationCard().model_dump(),
        "structural": StructuralCard().model_dump(),
        "requirements": [
            {
                "id": "req-001",
                "text": (
                    "Update parseInstalledPackagesLine to append warnings for "
                    "unparseable source RPM filenames, continue processing, "
                    "produce the binary package, and skip the source package."
                ),
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "scanner/redhatbase.go:577-629",
                    "scanner/redhatbase.go:528-575",
                    "models/scanresults.go:20-52",
                ],
                "findings": (
                    "parseInstalledPackagesLine calls splitFileName on fields[5] "
                    "before constructing the binary package. If splitFileName "
                    "fails, it returns (nil,nil,error), so it does not return "
                    "the binary package. There is no warning propagation path "
                    "from this parser into models.ScanResult.Warnings and no "
                    "warnings output parameter."
                ),
                "scoped_evidence": {
                    "localization": {
                        "exact_code_regions": [
                            "scanner/redhatbase.go:577-629",
                            "scanner/redhatbase.go:528-575",
                            "models/scanresults.go:20-52",
                        ],
                        "call_chain_context": [
                            "parseInstalledPackages -> parseInstalledPackagesLine -> splitFileName",
                            "parseRpmQfLine -> parseInstalledPackagesLine -> splitFileName",
                        ],
                    }
                },
            },
            {
                "id": "req-002",
                "text": (
                    "Implement handling in splitFileName to correctly parse RPM "
                    "filenames including epoch, producing a binary version with "
                    "epoch:version and a source version with epoch:version-release."
                ),
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "scanner/redhatbase.go:577-626",
                    "scanner/redhatbase.go:689-712",
                ],
                "findings": (
                    "splitFileName does not parse ':' as an epoch delimiter. "
                    "Callers currently prepend fields[1] to versions, so changing "
                    "splitFileName requires co-editing callers to avoid double-epoch "
                    "versions when filename epoch and fields[1] interact."
                ),
            }
        ],
    })
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-001"],
                conflicting_field="repair_targets",
                explanation="req-001 sufficiency FAIL: warning propagation repair_targets are missing",
            ),
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-001", "req-002"],
                conflicting_field="<cross-req>",
                explanation="double-epoch risk between SourceRPM fallback and splitFileName epoch parsing",
            )
        ],
        conflicts=[
            ClosureConflict(
                left_requirement_id="req-001",
                right_requirement_id="req-002",
                conflicting_field="<cross-req>",
                shared_evidence=["scanner/redhatbase.go"],
                explanation="double-epoch risk",
                recommended_recheck_side="right",
            )
        ],
    )

    validate_closure_conflicts(evidence, verdict)
    assert verdict.verdict == "CLOSURE_APPROVED"
    sufficiency = next(f for f in verdict.dimension_findings if f.dimension == "sufficiency")
    assert sufficiency.status == "PASS"
    consistency = next(f for f in verdict.dimension_findings if f.dimension == "consistency")
    assert consistency.status == "PASS"
    assert verdict.conflicts == []
    assert "RPM SourceRPM parse failure path" in sufficiency.explanation


def test_audit_pipeline_sufficiency_gaps_are_normalized_when_concrete():
    evidence = EvidenceCards.model_validate({
        "symptom": SymptomCard().model_dump(),
        "constraint": ConstraintCard().model_dump(),
        "localization": LocalizationCard().model_dump(),
        "structural": StructuralCard().model_dump(),
        "requirements": [
            {
                "id": "req-004",
                "text": (
                    "Server startup should provision any enabled audit sinks "
                    "and register an OpenTelemetry batch span processor when "
                    "at least one sink is enabled, using buffer.capacity and "
                    "buffer.flush_period to control batching behavior."
                ),
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "internal/cmd/grpc.go:85-186",
                    "internal/cmd/grpc.go:214-227",
                    "internal/config/config.go:39-50",
                    "internal/config/tracing.go:14-44",
                ],
                "findings": (
                    "NewGRPCServer builds tracing with "
                    "tracesdk.WithBatcher and registers shutdown via "
                    "onShutdown, but there is no audit sink subsystem, no "
                    "provision enabled sinks path, and no audit buffer "
                    "batching config."
                ),
                "scoped_evidence": {
                    "localization": {
                        "exact_code_regions": [
                            "internal/cmd/grpc.go:72-185",
                            "internal/cmd/grpc.go:307-323",
                        ],
                        "call_chain_context": [
                            "cmd/flipt/main.go -> NewGRPCServer -> onShutdown"
                        ],
                    },
                    "constraint": {
                        "similar_implementation_patterns": [
                            "Tracing lifecycle pattern uses tracesdk.WithBatcher in NewGRPCServer and onShutdown."
                        ]
                    },
                },
            },
            {
                "id": "req-005",
                "text": (
                    "The gRPC audit middleware should, after successful RPCs, "
                    "emit an audit event for create, update, and delete "
                    "operations, attaching the event to the current span."
                ),
                "origin": "requirements",
                "verdict": "AS_IS_VIOLATED",
                "evidence_locations": [
                    "internal/cmd/grpc.go:214-266",
                    "internal/server/middleware/grpc/middleware.go:23-235",
                    "internal/server/flag.go:87-135",
                    "internal/server/segment.go:65-113",
                    "internal/server/rule.go:65-122",
                    "internal/server/namespace.go:65-113",
                ],
                "findings": (
                    "The gRPC unary chain uses WithUnaryServerChain and "
                    "otelgrpc.UnaryServerInterceptor, but there is no audit "
                    "interceptor and mutation handlers do not emit any audit "
                    "events or attach them to the current span."
                ),
                "scoped_evidence": {
                    "localization": {
                        "call_chain_context": [
                            "grpc startup -> WithUnaryServerChain -> handler -> mutation handlers"
                        ],
                    },
                    "constraint": {
                        "behavioral_constraints": [
                            "Span is available via trace.SpanFromContext after successful RPCs."
                        ]
                    },
                },
            },
        ],
    })
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-004"],
                conflicting_field="repair_targets",
                explanation="req-004 sufficiency FAIL: repair_targets are not concretely localized",
            ),
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-005"],
                conflicting_field="repair_targets",
                explanation="req-005 sufficiency FAIL: middleware integration repair_targets missing",
            ),
        ],
    )

    validate_closure_conflicts(evidence, verdict)

    assert verdict.verdict == "CLOSURE_APPROVED"
    assert all(f.status == "PASS" for f in verdict.dimension_findings)
    assert "audit startup/middleware integration points" in verdict.dimension_findings[0].explanation
