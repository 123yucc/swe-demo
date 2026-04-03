"""
Custom MCP tools for the evidence pipeline:

1. submit_extracted_evidence — lets the Parser agent submit initial EvidenceCards.
2. update_localization — lets the Orchestrator persist confirmed code regions found
   by Deep Search back to the evidence_cards.json file on disk.

Both tools use module-level state so the calling Python code can retrieve
results after the agent loop finishes.
"""

from pathlib import Path
import re
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
                "missing_elements_to_implement": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Elements required by specifications but entirely absent "
                        "from the current codebase (interfaces, classes, methods). "
                        "Explicitly listed to prevent downstream agents from "
                        "hallucinating they already exist."
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
        "Persist confirmed defect locations AND structural/constraint findings "
        "from Deep Search back to evidence_cards.json.  Call this as soon as "
        "you extract findings from a Deep Search report — do NOT wait until "
        "final closure.  You MUST include ALL fields that Deep Search reported, "
        "not just exact_code_regions."
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
                "Caller-Callee chains discovered by Deep Search, formatted as "
                "'A -> B -> C' strings."
            ),
        },
        "dataflow_relevant_uses": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Def-Use relationships discovered by Deep Search."
            ),
        },
        "must_co_edit_relations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Co-edit dependencies discovered by Deep Search: 'If A changes "
                "→ B must also change'."
            ),
        },
        "dependency_propagation": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Cross-cutting dependency paths discovered by Deep Search "
                "(interface/package/config relationships)."
            ),
        },
        "missing_elements_to_implement": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "TO-BE elements confirmed absent from the codebase by Deep "
                "Search. These are interfaces/classes/methods required by "
                "specifications but not yet implemented."
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
    """Write confirmed localization, structural, and constraint data back to
    the evidence JSON file."""
    global _submitted_evidence, _evidence_json_path

    # Localization fields
    exact_code_regions: list[str] = args.get("exact_code_regions", [])
    suspect_entities: list[str] = args.get("suspect_entities", [])
    call_chain_context: list[str] = args.get("call_chain_context", [])
    dataflow_relevant_uses: list[str] = args.get("dataflow_relevant_uses", [])

    # Structural fields
    must_co_edit_relations: list[str] = args.get("must_co_edit_relations", [])
    dependency_propagation: list[str] = args.get("dependency_propagation", [])

    # Constraint field
    missing_elements: list[str] = args.get("missing_elements_to_implement", [])

    if _submitted_evidence is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No evidence cards in memory. Run the parser first.",
                }
            ]
        }

    def _normalize_exact_code_region(region: str) -> str:
        """Normalize path:line strings to repo-root-relative style."""
        normalized = region.strip().replace("\\", "/")
        normalized = re.sub(r"^(?:\./)?repo/", "", normalized)
        return normalized

    def _extract_symbol_candidates(text: str) -> set[str]:
        """Extract method/class-like names from a free-form evidence entry."""
        candidates: set[str] = set()
        value = text.strip()
        if not value:
            return candidates

        for match in re.findall(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\b", value):
            candidates.add(match.lower())

        for match in re.findall(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", value):
            candidates.add(match.lower())

        for match in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(?", value):
            low = match.lower()
            candidates.add(low)
            if "." in low:
                candidates.add(low.rsplit(".", 1)[-1])

        return candidates

    # Merge — union of existing + new values, preserving order.
    def _merge(existing: list[str], new: list[str]) -> list[str]:
        seen = set(existing)
        merged = list(existing)
        for v in new:
            if v not in seen:
                merged.append(v)
                seen.add(v)
        return merged

    normalized_exact_code_regions = [
        _normalize_exact_code_region(v) for v in exact_code_regions
    ]

    # Update LocalizationCard
    loc = _submitted_evidence.localization
    _submitted_evidence.localization = LocalizationCard(
        suspect_entities=_merge(loc.suspect_entities, suspect_entities),
        exact_code_regions=_merge(
            loc.exact_code_regions, normalized_exact_code_regions
        ),
        call_chain_context=_merge(loc.call_chain_context, call_chain_context),
        dataflow_relevant_uses=_merge(
            loc.dataflow_relevant_uses, dataflow_relevant_uses
        ),
    )

    # Update StructuralCard
    if must_co_edit_relations or dependency_propagation:
        struc = _submitted_evidence.structural
        _submitted_evidence.structural = StructuralCard(
            must_co_edit_relations=_merge(
                struc.must_co_edit_relations, must_co_edit_relations
            ),
            dependency_propagation=_merge(
                struc.dependency_propagation, dependency_propagation
            ),
        )

    # Update ConstraintCard (missing_elements_to_implement only)
    if missing_elements:
        con = _submitted_evidence.constraint
        con.missing_elements_to_implement = _merge(
            con.missing_elements_to_implement, missing_elements
        )

    # Contradiction guard: a method/class that already appears in suspects
    # should not remain in missing_elements_to_implement.
    con = _submitted_evidence.constraint
    loc = _submitted_evidence.localization
    suspect_symbols: set[str] = set()
    for entity in loc.suspect_entities:
        suspect_symbols.update(_extract_symbol_candidates(entity))

    filtered_missing: list[str] = []
    removed_missing: list[str] = []
    for item in con.missing_elements_to_implement:
        item_symbols = _extract_symbol_candidates(item)
        if item_symbols and any(symbol in suspect_symbols for symbol in item_symbols):
            removed_missing.append(item)
            continue
        filtered_missing.append(item)

    if removed_missing:
        con.missing_elements_to_implement = filtered_missing
        for item in removed_missing:
            print(
                "[WARNING contradiction resolved] removed from "
                f"missing_elements_to_implement because it appears in "
                f"suspect_entities: {item}"
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
                    f"Evidence updated: exact_code_regions={exact_code_regions}, "
                    f"entities={suspect_entities}, "
                    f"call_chains={call_chain_context}, "
                    f"dataflow={dataflow_relevant_uses}, "
                    f"co_edits={must_co_edit_relations}, "
                    f"dep_propagation={dependency_propagation}, "
                    f"missing_elements={missing_elements}.{saved_msg}"
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

