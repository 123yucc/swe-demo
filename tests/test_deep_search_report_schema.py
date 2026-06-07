"""Schema guard for DeepSearchReport.

DeepSearchReport should expose only the per-requirement verdict fields and
the AS-IS observation fields that the orchestrator persists through
update_localization. Historical/debug-only fields must not re-enter the main
structured output schema because downstream agents do not consume them.
"""

from src.models.report import DeepSearchReport


def test_deep_search_report_has_no_debug_only_fields():
    forbidden = {
        "boundary_analysis",
        "confirmed_defect_locations",
        "new_suspects",
        "ruled_out_suspects",
        "open_questions",
        "missing_elements_to_implement",
    }

    assert forbidden.isdisjoint(DeepSearchReport.model_fields)


def test_deep_search_report_persisted_observation_fields_are_explicit():
    requirement_fields = {
        "target_requirement_id",
        "requirement_verdict",
        "requirement_findings",
        "requirement_evidence_locations",
    }
    persisted_observation_fields = {
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
    }
    report_observation_fields = set(DeepSearchReport.model_fields) - requirement_fields

    assert report_observation_fields == persisted_observation_fields
