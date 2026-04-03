"""
Custom MCP tools for the evidence pipeline:

1. submit_extracted_evidence — lets the Parser agent submit initial EvidenceCards.
2. update_localization — lets the Orchestrator persist confirmed code regions found
   by Deep Search back to the evidence_cards.json file on disk.

Both tools use module-level state so the calling Python code can retrieve
results after the agent loop finishes.
"""

from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    StructuralCard,
    SymptomCard,
)
from src.models.context import EvidenceCards

# Shared in-memory slot; populated by the tool, read by run_parser().
_submitted_evidence: EvidenceCards | None = None

# Path where evidence_cards.json lives; set by the orchestrator engine before
# launching the orchestrator agent so update_localization can write it back.
_evidence_json_path: Path | None = None


def set_evidence_json_path(path: str | Path) -> None:
    global _evidence_json_path
    _evidence_json_path = Path(path)


def get_evidence_json_path() -> Path | None:
    return _evidence_json_path


def get_submitted_evidence() -> EvidenceCards | None:
    return _submitted_evidence


def reset_submitted_evidence() -> None:
    global _submitted_evidence
    _submitted_evidence = None


# Flat inline schema — avoids $ref/$defs which the in-process MCP server
# cannot resolve, causing nested objects to arrive as strings.
_EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["symptom", "constraint", "localization", "structural"],
    "properties": {
        "symptom": {
            "type": "object",
            "description": "Observable failure symptoms and repair expectations.",
            "properties": {
                "observable_failures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Visible symptoms: error messages, exception types, "
                        "stack traces, observable anomalies."
                    ),
                },
                "repair_targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the fix should achieve (expected behaviour).",
                },
                "regression_expectations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Existing correct behaviours that must NOT be broken."
                    ),
                },
            },
        },
        "constraint": {
            "type": "object",
            "description": "Constraints and reference patterns the fix must respect.",
            "properties": {
                "semantic_boundaries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "API contracts, docstring/annotation constraints.",
                },
                "behavioral_constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Assertions, invariants, schema constraints, behavioural rules."
                    ),
                },
                "backward_compatibility": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Backward-compatibility requirements.",
                },
                "similar_implementation_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Existing similar API implementations as reference baselines."
                    ),
                },
            },
        },
        "localization": {
            "type": "object",
            "description": (
                "Code locations suspected to contain the defect, with "
                "program-analysis context."
            ),
            "properties": {
                "suspect_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Suspected files, classes, functions, or variables."
                    ),
                },
                "exact_code_regions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact code lines or hunks (e.g. 'auth.py:42-58')."
                    ),
                },
                "call_chain_context": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Call chains — Caller-Callee relationships around the defect."
                    ),
                },
                "dataflow_relevant_uses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Variable Def-Use relationships relevant to the defect."
                    ),
                },
            },
        },
        "structural": {
            "type": "object",
            "description": "Co-edit dependencies and propagation paths.",
            "properties": {
                "must_co_edit_relations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Co-edit dependencies: modifying A requires updating B."
                    ),
                },
                "dependency_propagation": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Interface/package/config dependency paths."
                    ),
                },
            },
        },
    },
}


@tool(
    "submit_extracted_evidence",
    "Submit the structured evidence extracted from the issue artifacts. "
    "Call this exactly once after you have read all documents.",
    _EVIDENCE_SCHEMA,
)
async def submit_extracted_evidence(args: dict[str, Any]) -> dict[str, Any]:
    """Receive the agent's extracted evidence and store it."""
    global _submitted_evidence
    _submitted_evidence = EvidenceCards(
        symptom=SymptomCard(**args["symptom"]),
        constraint=ConstraintCard(**args.get("constraint", {})),
        localization=LocalizationCard(**args.get("localization", {})),
        structural=StructuralCard(**args.get("structural", {})),
    )
    return {
        "content": [
            {
                "type": "text",
                "text": "Evidence received and stored successfully.",
            }
        ]
    }


_UPDATE_LOCALIZATION_SCHEMA = {
    "type": "object",
    "description": (
        "Persist confirmed defect locations found by Deep Search back to the "
        "evidence_cards.json file.  Call this as soon as you extract findings "
        "from a Deep Search report — do NOT wait until final closure."
    ),
    "properties": {
        "exact_code_regions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exact line numbers or ranges in 'file.py:N' or 'file.py:N-M' form, "
                "e.g. ['backend/models/face_detector.py:120', "
                "'backend/models/face_detector.py:126']."
            ),
        },
        "suspect_entities": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files, classes, functions, or variables confirmed or updated "
                "by Deep Search."
            ),
        },
        "call_chain_context": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Caller-Callee chains discovered by Deep Search."
            ),
        },
        "dataflow_relevant_uses": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Def-Use relationships discovered by Deep Search."
            ),
        },
    },
    "required": ["exact_code_regions"],
}


@tool(
    "update_localization",
    (
        "Persist confirmed defect locations and program-analysis context "
        "discovered by Deep Search into the evidence_cards.json on disk. "
        "You MUST call this tool before declaring evidence closure whenever "
        "Deep Search has identified concrete code regions."
    ),
    _UPDATE_LOCALIZATION_SCHEMA,
)
async def update_localization(args: dict[str, Any]) -> dict[str, Any]:
    """Write confirmed localization data back to the evidence JSON file."""
    global _submitted_evidence, _evidence_json_path

    exact_code_regions: list[str] = args.get("exact_code_regions", [])
    suspect_entities: list[str] = args.get("suspect_entities", [])
    call_chain_context: list[str] = args.get("call_chain_context", [])
    dataflow_relevant_uses: list[str] = args.get("dataflow_relevant_uses", [])

    if _submitted_evidence is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No evidence cards in memory. Run the parser first.",
                }
            ]
        }

    # Merge — union of existing + new values, preserving order.
    def _merge(existing: list[str], new: list[str]) -> list[str]:
        seen = set(existing)
        return existing + [v for v in new if v not in seen]

    loc = _submitted_evidence.localization
    _submitted_evidence.localization = LocalizationCard(
        suspect_entities=_merge(loc.suspect_entities, suspect_entities),
        exact_code_regions=_merge(loc.exact_code_regions, exact_code_regions),
        call_chain_context=_merge(loc.call_chain_context, call_chain_context),
        dataflow_relevant_uses=_merge(
            loc.dataflow_relevant_uses, dataflow_relevant_uses
        ),
    )

    if _evidence_json_path is not None:
        _evidence_json_path.write_text(
            _submitted_evidence.model_dump_json(indent=2), encoding="utf-8"
        )
        saved_msg = f" Saved to {_evidence_json_path}."
    else:
        saved_msg = " (evidence_json_path not set — in-memory only)"

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Localization updated: exact_code_regions={exact_code_regions}, "
                    f"entities={suspect_entities}, "
                    f"call_chains={call_chain_context}, "
                    f"dataflow={dataflow_relevant_uses}.{saved_msg}"
                ),
            }
        ]
    }


# MCP server instance — passed to ClaudeAgentOptions.mcp_servers
ingestion_server = create_sdk_mcp_server(
    name="ingestion",
    version="1.0.0",
    tools=[submit_extracted_evidence],
)

# Evidence-update server — passed to the Orchestrator's ClaudeAgentOptions.
evidence_server = create_sdk_mcp_server(
    name="evidence",
    version="1.0.0",
    tools=[update_localization],
)
