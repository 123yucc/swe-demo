import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key, value in payload.items():
    if isinstance(value, str):
        print(key, "str", len(value))
    elif isinstance(value, (list, dict)):
        print(key, type(value).__name__, len(value))
    else:
        print(key, type(value).__name__)
