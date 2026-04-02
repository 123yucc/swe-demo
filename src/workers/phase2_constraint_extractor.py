"""Phase2 constraint extractor worker."""

from __future__ import annotations

import ast
from typing import Any, Dict

from ._common import load_json, resolve_instance_dir, save_json


def extract_constraint_evidence(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    evidence_dir = instance_dir / "evidence"
    repo_dir = instance_dir / "repo"
    v2_dir = evidence_dir / "card_versions" / "v2"

    path = evidence_dir / "constraint_card.json"
    if not path.exists():
        raise FileNotFoundError("constraint_card.json missing; phase1 must run first")

    card = load_json(path)
    type_constraints: Dict[str, str] = card.get("type_constraints", {})

    if repo_dir.exists():
        for py_file in repo_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    if node.returns is not None:
                        type_constraints[f"{node.name}.return"] = ast.unparse(node.returns)
                    for arg in node.args.args:
                        if arg.annotation is not None:
                            type_constraints[f"{node.name}.{arg.arg}"] = ast.unparse(arg.annotation)

    card["version"] = 2
    card["updated_by"] = "constraint-extractor"
    card["type_constraints"] = type_constraints
    card["sufficiency_status"] = "sufficient" if type_constraints else "partial"
    card["sufficiency_notes"] = "phase2 constraint refined"

    save_json(path, card)
    save_json(v2_dir / "constraint_card_v2.json", card)
    return {"card": "constraint", "version": 2, "type_constraints": len(type_constraints)}
