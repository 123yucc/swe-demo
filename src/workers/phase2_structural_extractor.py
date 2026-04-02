"""Phase2 structural extractor worker."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from ._common import load_json, resolve_instance_dir, save_json


def extract_structural_evidence(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    v2_dir = evidence_dir / "card_versions" / "v2"

    path = evidence_dir / "structural_card.json"
    loc_path = evidence_dir / "localization_card.json"
    if not path.exists() or not loc_path.exists():
        raise FileNotFoundError("structural/localization card missing; phase1/phase2 required")

    card = load_json(path)
    localization = load_json(loc_path)
    locations = localization.get("candidate_locations", [])

    file_to_symbols: Dict[str, List[str]] = defaultdict(list)
    for loc in locations:
        fp = loc.get("file_path")
        sym = loc.get("symbol_name")
        if fp and sym:
            file_to_symbols[fp].append(sym)

    edges: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    for file_path, symbols in file_to_symbols.items():
        if len(symbols) < 2:
            continue
        for i in range(len(symbols) - 1):
            edges.append(
                {
                    "from_entity": symbols[i],
                    "to_entity": symbols[i + 1],
                    "edge_type": "co_file",
                    "strength": "medium",
                    "evidence_source": [
                        {
                            "source_type": "repo",
                            "source_path": file_path,
                            "matching_detail": {},
                            "confidence_contribution": 0.7,
                        }
                    ],
                }
            )
        groups.append(
            {
                "group_id": f"group_{abs(hash(file_path)) % 100000}",
                "entities": sorted(set(symbols)),
                "reason": f"same file: {file_path}",
                "evidence_source": [
                    {
                        "source_type": "repo",
                        "source_path": file_path,
                        "matching_detail": {},
                        "confidence_contribution": 0.7,
                    }
                ],
            }
        )

    card["version"] = 2
    card["updated_by"] = "structural-extractor"
    card["dependency_edges"] = edges
    card["co_edit_groups"] = groups
    card["propagation_risks"] = ["changes may impact related symbols in same file"] if groups else []
    card["sufficiency_status"] = "sufficient" if groups else "partial"
    card["sufficiency_notes"] = "phase2 structural refined"

    save_json(path, card)
    save_json(v2_dir / "structural_card_v2.json", card)
    return {"card": "structural", "version": 2, "groups": len(groups)}
