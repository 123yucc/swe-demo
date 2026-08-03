import asyncio
import json
import sys
from pathlib import Path

from src.agents.closure_checker_agent import _run_closure_checker_async
from src.models.context import EvidenceCards
from src.orchestrator.audit import build_audit_manifest

evidence_path = Path(sys.argv[1])
repo_dir = Path(sys.argv[2])
evidence = EvidenceCards.model_validate_json(evidence_path.read_text(encoding="utf-8"))
manifest = build_audit_manifest(evidence)
verdict = asyncio.run(_run_closure_checker_async(evidence, manifest, repo_dir))
print(json.dumps(verdict.model_dump(), ensure_ascii=False, indent=2))
