"""Schema guard for DeepSearchReport.

DeepSearchReport should expose only the per-requirement verdict fields and
the AS-IS observation fields that the orchestrator persists through
update_localization. Historical/debug-only fields must not re-enter the main
structured output schema because downstream agents do not consume them.
"""

from src.models.report import DeepSearchReport
from src.tools.ingestion_tools import DEEP_SEARCH_OWNED_FIELDS


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
    # The observation fields exposed by DeepSearchReport must match exactly the
    # deep-search-owned fields that update_localization persists. Binding the
    # expectation to DEEP_SEARCH_OWNED_FIELDS (the single source of truth) means
    # a field added on one side but not the other fails here — the gap that
    # previously left consistency_anchors generated-but-never-persisted.
    persisted_observation_fields = set(DEEP_SEARCH_OWNED_FIELDS)
    report_observation_fields = set(DeepSearchReport.model_fields) - requirement_fields

    assert report_observation_fields == persisted_observation_fields


def test_deep_search_report_normalizes_similar_implementation_pattern_objects():
    report = DeepSearchReport.model_validate(
        {
            "target_requirement_id": "req-001",
            "requirement_verdict": "TO_BE_MISSING",
            "requirement_findings": "missing implementation",
            "requirement_evidence_locations": [],
            "similar_implementation_patterns": [
                {
                    "description": "Existing helper follows the desired flow",
                    "location": "src/example.py:10-20",
                },
                "plain string pattern",
            ],
        }
    )

    assert report.similar_implementation_patterns == [
        "Existing helper follows the desired flow | src/example.py:10-20",
        "plain string pattern",
    ]
