"""Phase2 cross-card enhancer worker."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from ._common import load_json, resolve_instance_dir, save_json


def enhance_all_cards(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    cards = ["symptom", "localization", "constraint", "structural"]

    enhanced = {}
    for card_type in cards:
        path = evidence_dir / f"{card_type}_card.json"
        if not path.exists():
            raise FileNotFoundError(f"missing card for enhancer: {path}")
        payload = load_json(path)
        payload["updated_by"] = "llm-enhancer"
        note = payload.get("sufficiency_notes", "")
        payload["sufficiency_notes"] = f"{note}; enhanced at {datetime.utcnow().isoformat()}".strip("; ")
        save_json(path, payload)
        enhanced[card_type] = payload.get("version", 0)

    save_json(evidence_dir / "phase2_summary.json", {"phase": "phase2", "enhanced": enhanced})
    return {"enhanced_cards": enhanced}
