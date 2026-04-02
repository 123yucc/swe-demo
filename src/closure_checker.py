"""Phase3 closure checker worker implementation."""

from __future__ import annotations

from typing import Any, Dict, List

from .workers._common import load_json, resolve_instance_dir, save_json


def check_evidence_closure(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    closure_dir = instance_dir / "closure"

    cards = {}
    for name in ["symptom", "localization", "constraint", "structural"]:
        path = evidence_dir / f"{name}_card.json"
        if not path.exists():
            raise FileNotFoundError(f"missing evidence card: {path}")
        cards[name] = load_json(path)

    gaps: List[Dict[str, Any]] = []
    for name, card in cards.items():
        status = str(card.get("sufficiency_status", "unknown"))
        if status in {"insufficient", "unknown"}:
            gaps.append({"id": name, "card_type": name, "description": f"{name} is {status}"})

    passed = len(gaps) == 0
    report = {"passed": passed, "overall_status": "allow" if passed else "block", "gaps": gaps}
    save_json(closure_dir / "closure_report.json", report)
    return report
