"""
One-shot resume script: continue swe_issue_003 from the saved evidence
without re-running the 11-round deep-search phase.

Loads workdir/swe_issue_003/outputs/{evidence.json, working_memory.json},
re-populates ingestion_tools module state (working memory + scoped store),
then enters run_pipeline in EvidenceRefining so the closure-checker fix
can be exercised end-to-end.

Usage:
    python -m scripts.resume_issue003
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src import config as _config  # noqa: F401  (side-effect: loads .env)
from src.models.context import EvidenceCards
from src.models.memory import SharedWorkingMemory
from src.orchestrator.engine import run_pipeline_from_evidence
from src.tools import ingestion_tools


ISSUE_DIR = Path("workdir/swe_issue_003")
REPO_DIR = (ISSUE_DIR / "repo").resolve()
OUTPUT_DIR = (ISSUE_DIR / "outputs").resolve()
EVIDENCE_JSON = OUTPUT_DIR / "evidence.json"
WM_JSON = OUTPUT_DIR / "working_memory.json"
INSTANCE_JSON = (ISSUE_DIR / "artifacts" / "instance_metadata.json").resolve()


def _rebuild_scoped_store(evidence: EvidenceCards) -> None:
    """Rebuild _scoped_store from a fully populated EvidenceCards.

    Resume-only heuristic: at resume time we don't know which scope each
    aggregate entry originally came from.  We dump EVERY aggregate list under
    a synthetic 'resume' scope so that _rebuild_aggregate_view() leaves the
    aggregate view unchanged if it fires.  Requirement-level rework will
    re-scope properly on the next deep-search round.
    """
    bucket: dict[str, list[str]] = {}
    for name in ("suspect_entities", "exact_code_regions",
                 "call_chain_context", "dataflow_relevant_uses"):
        vals = getattr(evidence.localization, name, [])
        if vals:
            bucket[name] = list(vals)
    for name in ("must_co_edit_relations", "dependency_propagation"):
        vals = getattr(evidence.structural, name, [])
        if vals:
            bucket[name] = list(vals)
    for name in ("behavioral_constraints", "semantic_boundaries",
                 "backward_compatibility", "similar_implementation_patterns"):
        vals = getattr(evidence.constraint, name, [])
        if vals:
            bucket[name] = list(vals)
    if bucket:
        ingestion_tools._scoped_store["__resume__"] = bucket


async def _main() -> None:
    instance = json.loads(INSTANCE_JSON.read_text(encoding="utf-8"))
    instance_id = instance["instance_id"]

    # Load saved artifacts
    evidence = EvidenceCards.model_validate_json(
        EVIDENCE_JSON.read_text(encoding="utf-8")
    )
    wm = SharedWorkingMemory.model_validate_json(
        WM_JSON.read_text(encoding="utf-8")
    )
    # Ensure evidence_cards inside WM matches the on-disk evidence.json
    wm.evidence_cards = evidence

    # Re-populate ingestion_tools module globals
    ingestion_tools.set_repo_root(REPO_DIR)
    ingestion_tools._working_memory = wm
    ingestion_tools._scoped_store = {}
    _rebuild_scoped_store(evidence)
    ingestion_tools.set_evidence_json_path(EVIDENCE_JSON)

    wm.record_action(
        phase="resume",
        outcome="loaded_evidence_and_memory",
    )

    print(f"[resume] Instance:     {instance_id}")
    print(f"[resume] Repo dir:     {REPO_DIR}")
    print(f"[resume] Output dir:   {OUTPUT_DIR}")
    print(f"[resume] Requirements: {len(evidence.requirements)}")
    for r in evidence.requirements:
        print(f"  - {r.id} origin={r.origin} verdict={r.verdict} "
              f"locs={len(r.evidence_locations)}")

    await run_pipeline_from_evidence(
        issue_id=instance_id,
        repo_dir=REPO_DIR,
        output_dir=OUTPUT_DIR,
    )


if __name__ == "__main__":
    asyncio.run(_main())
