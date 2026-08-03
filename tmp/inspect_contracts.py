import json
import sys
from pathlib import Path

from src.agents.contract_parser import build_requirement_ledger, validate_ledger_coverage

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
parts = []
for key, heading in (
    ("problem_statement", "Problem Statement"),
    ("requirements", "Requirements"),
    ("interface", "New interfaces introduced"),
):
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        parts.append(f"# {heading}:\n{value.strip()}\n")
text = "\n".join(parts)
print(f"source_fields=problem_statement,requirements,interface chars={len(text)}")

items = build_requirement_ledger(text)
validate_ledger_coverage(text, items)
print(f"contracts={len(items)}")
for item in items:
    print(item.id, item.contract_kind, item.explicit_paths, item.text.splitlines()[0][:100])
