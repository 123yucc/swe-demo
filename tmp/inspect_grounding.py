import json
import sys
from pathlib import Path

from src.models.context import EvidenceCards
from src.orchestrator.grounding import run_static_grounding

evidence = EvidenceCards.model_validate_json(Path(sys.argv[1]).read_text())
for failure in run_static_grounding(evidence, Path(sys.argv[2])):
    print(failure.render())
