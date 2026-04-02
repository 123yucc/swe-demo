"""Phase2 localization extractor worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ._common import load_json, resolve_instance_dir, save_json


def extract_localization_evidence(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    repo_dir = instance_dir / "repo"
    v2_dir = evidence_dir / "card_versions" / "v2"

    loc_path = evidence_dir / "localization_card.json"
    symptom_path = evidence_dir / "symptom_card.json"
    if not loc_path.exists() or not symptom_path.exists():
        raise FileNotFoundError("phase1 cards missing for localization extraction")

    card = load_json(loc_path)
    symptom = load_json(symptom_path)
    entities = symptom.get("mentioned_entities", [])

    candidates: List[Dict[str, Any]] = []
    if repo_dir.exists():
        py_files = list(repo_dir.rglob("*.py"))
        for ent in entities[:8]:
            name = ent.get("name")
            if not name:
                continue
            for py_file in py_files:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if name in content:
                    rel = str(py_file.relative_to(instance_dir)).replace("\\", "/")
                    line = next((idx + 1 for idx, text in enumerate(content.splitlines()) if name in text), 1)
                    candidates.append(
                        {
                            "file_path": rel,
                            "symbol_name": name,
                            "symbol_type": ent.get("type", "symbol"),
                            "region_start": line,
                            "region_end": line,
                            "evidence_source": ent.get("evidence_source", []),
                            "computed_confidence": 0.8,
                        }
                    )
                    break

    card["version"] = 2
    card["updated_by"] = "localization-extractor"
    card["candidate_locations"] = candidates or card.get("candidate_locations", [])
    card["sufficiency_status"] = "sufficient" if candidates else "partial"
    card["sufficiency_notes"] = "phase2 localization refined"

    save_json(loc_path, card)
    save_json(v2_dir / "localization_card_v2.json", card)
    return {"card": "localization", "version": 2, "candidates": len(card.get("candidate_locations", []))}
