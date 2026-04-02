"""Phase2 symptom extractor worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ._common import load_json, resolve_instance_dir, save_json


def extract_symptom_evidence(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    v2_dir = evidence_dir / "card_versions" / "v2"

    symptom_path = evidence_dir / "symptom_card.json"
    if not symptom_path.exists():
        raise FileNotFoundError("symptom_card.json missing; phase1 must run first")

    card = load_json(symptom_path)
    card["version"] = 2
    card["updated_by"] = "symptom-extractor"
    card["sufficiency_status"] = "sufficient"
    base_note = card.get("sufficiency_notes", "")
    card["sufficiency_notes"] = f"{base_note}; phase2 symptom refined".strip("; ")

    save_json(symptom_path, card)
    save_json(v2_dir / "symptom_card_v2.json", card)
    return {"card": "symptom", "version": 2, "status": "ok"}
