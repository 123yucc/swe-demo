"""Pipeline entrypoint for end-to-end repair workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from claude_agent_sdk import AgentDefinition, query
from pydantic import BaseModel

from .adapters import create_evidence_extraction_options
from .evidence_cards import ConstraintCard, LocalizationCard, StructuralCard, SymptomCard
from .workers import registry as agent_config


CORE_ARTIFACT_ALIASES: Dict[str, List[str]] = {
    "problem_statement": ["problem_statement.md"],
    "requirements": ["requirements.md"],
    "interfaces": ["new_interfaces.md", "interface.md"],
    "expected_behavior": ["expected_and_current_behavior.md", "expected_and current_behavior.md"],
}


class EvidenceCardsOutput(BaseModel):
    """Structured output contract returned by Claude SDK."""

    symptom_card: SymptomCard
    localization_card: LocalizationCard
    constraint_card: ConstraintCard
    structural_card: StructuralCard


def _resolve_instance_dir(workspace_dir: str, instance_id: str) -> Path:
    workspace_path = Path(workspace_dir)
    if workspace_path.name == instance_id:
        return workspace_path
    return workspace_path / instance_id


def _collect_files(root: Path, max_count: int = 200) -> List[str]:
    if not root.exists():
        return []
    files = sorted(str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*") if path.is_file())
    return files[:max_count]


def _artifact_status(instance_dir: Path) -> Tuple[List[str], List[str], Dict[str, str], List[str]]:
    artifacts_root = instance_dir / "artifacts"
    files = _collect_files(artifacts_root, max_count=400)
    file_set = set(files)

    resolved_core_artifacts: Dict[str, str] = {}
    missing_core_artifacts: List[str] = []
    for artifact_key, aliases in CORE_ARTIFACT_ALIASES.items():
        matched = next((name for name in aliases if name in file_set), None)
        if matched is None:
            missing_core_artifacts.append(artifact_key)
        else:
            resolved_core_artifacts[artifact_key] = matched

    available_alias_files = sorted(resolved_core_artifacts.values())
    missing_alias_files: List[str] = []
    for artifact_key in missing_core_artifacts:
        missing_alias_files.extend(CORE_ARTIFACT_ALIASES[artifact_key])

    return available_alias_files, missing_alias_files, resolved_core_artifacts, missing_core_artifacts


def _build_prompt(
    *,
    base_prompt: str,
    instance_id: str,
    instance_dir: Path,
    artifact_files: Iterable[str],
    repo_files: Iterable[str],
    resolved_core_artifacts: Dict[str, str],
    missing_core_artifacts: Iterable[str],
    phase1_only: bool,
    phase2_only: bool,
) -> str:
    execution_mode = "full_phase1_phase2"
    if phase1_only:
        execution_mode = "phase1_only"
    elif phase2_only:
        execution_mode = "phase2_only"

    artifact_file_list = list(artifact_files)
    repo_file_list = list(repo_files)
    artifact_preview = artifact_file_list[:80]
    repo_preview = repo_file_list[:80]

    lines = [
        f"Task: Build high-quality evidence cards for instance {instance_id}.",
        f"Execution mode: {execution_mode}.",
        f"Instance workspace: {instance_dir}",
        "Artifacts directory: artifacts/",
        "Repository directory: repo/",
        "",
        "Canonical artifact contract (4 docs + repo):",
        "- problem_statement.md",
        "- requirements.md",
        "- new_interfaces.md (fallback: interface.md)",
        "- expected_and_current_behavior.md (fallback: expected_and current_behavior.md)",
        "",
        "Resolved core artifacts:",
        *[f"- {name}: {path}" for name, path in resolved_core_artifacts.items()],
        *([f"- missing: {name}" for name in missing_core_artifacts] if list(missing_core_artifacts) else ["- missing: none"]),
        "",
        "Required behavior:",
        "- Use subagents when useful: phase1_parser, symptom_extractor, localization_extractor, constraint_extractor, structural_extractor.",
        "- Read real artifacts/repo/tests before deciding claims.",
        "- Never fabricate paths, symbols, test names, or stack traces.",
        "- Populate sufficiency_status and sufficiency_notes based on actual evidence completeness.",
        "- Keep evidence_source fields traceable to concrete files whenever possible.",
        "",
        f"Artifact file index (showing {len(artifact_preview)} / {len(artifact_file_list)}):",
        *[f"- {path}" for path in artifact_preview],
        "",
        f"Repository file index (showing {len(repo_preview)} / {len(repo_file_list)}):",
        *[f"- {path}" for path in repo_preview],
        "",
        "User intent:",
        base_prompt,
    ]

    prompt_text = "\n".join(lines)
    max_prompt_chars = 18000
    if len(prompt_text) > max_prompt_chars:
        prompt_text = prompt_text[:max_prompt_chars] + "\n\n[truncated to avoid command-line length limits]"
    return prompt_text


def _build_agents(phase1_only: bool, phase2_only: bool) -> Dict[str, AgentDefinition]:
    agents_to_run: Dict[str, AgentDefinition] = {}

    if not phase2_only:
        phase1_def = agent_config.AGENT_DEFINITIONS["phase1_parser"]
        agents_to_run["phase1_parser"] = AgentDefinition(
            description=phase1_def["description"],
            prompt=phase1_def["prompt"],
            tools=phase1_def["tools"],
        )

    if not phase1_only:
        for agent_name in [
            "symptom_extractor",
            "localization_extractor",
            "constraint_extractor",
            "structural_extractor",
        ]:
            agent_def = agent_config.AGENT_DEFINITIONS[agent_name]
            agents_to_run[agent_name] = AgentDefinition(
                description=agent_def["description"],
                prompt=agent_def["prompt"],
                tools=agent_def["tools"],
            )

    return agents_to_run


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _persist_cards(instance_dir: Path, cards: EvidenceCardsOutput, version: int) -> Dict[str, str]:
    evidence_dir = instance_dir / "evidence"
    version_dir = evidence_dir / "card_versions" / f"v{version}"
    timestamp = datetime.now(timezone.utc).isoformat()

    cards.symptom_card.version = version
    cards.symptom_card.updated_at = timestamp
    cards.localization_card.version = version
    cards.localization_card.updated_at = timestamp
    cards.constraint_card.version = version
    cards.constraint_card.updated_at = timestamp
    cards.structural_card.version = version
    cards.structural_card.updated_at = timestamp

    path_map = {
        "symptom_card": evidence_dir / "symptom_card.json",
        "localization_card": evidence_dir / "localization_card.json",
        "constraint_card": evidence_dir / "constraint_card.json",
        "structural_card": evidence_dir / "structural_card.json",
    }

    data_map = {
        "symptom_card": cards.symptom_card.model_dump(mode="json"),
        "localization_card": cards.localization_card.model_dump(mode="json"),
        "constraint_card": cards.constraint_card.model_dump(mode="json"),
        "structural_card": cards.structural_card.model_dump(mode="json"),
    }

    for key, file_path in path_map.items():
        _write_json(file_path, data_map[key])
        snapshot_path = version_dir / f"{key}_v{version}.json"
        _write_json(snapshot_path, data_map[key])

    return {key: str(path_map[key]) for key in path_map}


def _build_phase_summary(
    *,
    instance_id: str,
    phase: str,
    version: int,
    artifact_files: List[str],
    available_artifacts: List[str],
    missing_artifacts: List[str],
    resolved_core_artifacts: Dict[str, str],
    missing_core_artifacts: List[str],
    cards: EvidenceCardsOutput,
) -> Dict[str, Any]:
    return {
        "instance_id": instance_id,
        "phase": phase,
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts_indexed": artifact_files,
        "available_artifacts": available_artifacts,
        "missing_artifacts": missing_artifacts,
        "core_artifact_contract": {
            "required": list(CORE_ARTIFACT_ALIASES.keys()),
            "resolved": resolved_core_artifacts,
            "missing": missing_core_artifacts,
        },
        "card_sufficiency": {
            "symptom": cards.symptom_card.sufficiency_status,
            "localization": cards.localization_card.sufficiency_status,
            "constraint": cards.constraint_card.sufficiency_status,
            "structural": cards.structural_card.sufficiency_status,
        },
    }


async def run_repair_workflow(
    workspace_dir: str,
    instance_id: str,
    prompt: str,
    phase1_only: bool = False,
    phase2_only: bool = False,
) -> Dict[str, Any]:
    """Run phase1/phase2 evidence extraction with structured Claude SDK output."""
    instance_dir = _resolve_instance_dir(workspace_dir, instance_id)
    artifact_root = instance_dir / "artifacts"
    repo_root = instance_dir / "repo"

    artifact_files = _collect_files(artifact_root, max_count=300)
    repo_files = _collect_files(repo_root, max_count=150)
    available_artifacts, missing_artifacts, resolved_core_artifacts, missing_core_artifacts = _artifact_status(instance_dir)

    prompt_for_agent = _build_prompt(
        base_prompt=prompt,
        instance_id=instance_id,
        instance_dir=instance_dir,
        artifact_files=artifact_files,
        repo_files=repo_files,
        resolved_core_artifacts=resolved_core_artifacts,
        missing_core_artifacts=missing_core_artifacts,
        phase1_only=phase1_only,
        phase2_only=phase2_only,
    )
    agents_to_run = _build_agents(phase1_only=phase1_only, phase2_only=phase2_only)

    final_result: Any = None
    structured_output: Any = None
    options = create_evidence_extraction_options(cwd=str(instance_dir), agents=agents_to_run, output_schema=EvidenceCardsOutput.model_json_schema())
    try:
        async for message in query(
            prompt=prompt_for_agent,
            options=options,
        ):
            if hasattr(message, "result"):
                final_result = getattr(message, "result")
            message_structured_output = getattr(message, "structured_output", None)
            if message_structured_output is not None:
                structured_output = message_structured_output
    except Exception as exc:
        return {
            "instance_id": instance_id,
            "prompt": prompt,
            "success": False,
            "final_state": "failed",
            "result": final_result,
            "error": f"Claude SDK execution failed: {exc}",
        }

    if structured_output is None:
        return {
            "instance_id": instance_id,
            "prompt": prompt,
            "success": False,
            "final_state": "failed",
            "result": final_result,
            "error": "Claude SDK did not return structured_output for evidence cards.",
        }

    cards = EvidenceCardsOutput.model_validate(structured_output)
    target_version = 1 if phase1_only else 2
    card_paths = _persist_cards(instance_dir, cards, version=target_version)

    evidence_dir = instance_dir / "evidence"
    if not phase2_only:
        phase1_summary = _build_phase_summary(
            instance_id=instance_id,
            phase="phase1",
            version=1,
            artifact_files=artifact_files,
            available_artifacts=available_artifacts,
            missing_artifacts=missing_artifacts,
            resolved_core_artifacts=resolved_core_artifacts,
            missing_core_artifacts=missing_core_artifacts,
            cards=cards,
        )
        _write_json(evidence_dir / "phase1_summary.json", phase1_summary)

    if not phase1_only:
        phase2_summary = _build_phase_summary(
            instance_id=instance_id,
            phase="phase2",
            version=target_version,
            artifact_files=artifact_files,
            available_artifacts=available_artifacts,
            missing_artifacts=missing_artifacts,
            resolved_core_artifacts=resolved_core_artifacts,
            missing_core_artifacts=missing_core_artifacts,
            cards=cards,
        )
        phase2_summary["repo_files_indexed"] = repo_files
        _write_json(evidence_dir / "phase2_summary.json", phase2_summary)

    return {
        "instance_id": instance_id,
        "prompt": prompt,
        "success": True,
        "final_state": "phase2_completed" if not phase1_only else "phase1_completed",
        "result": {
            "cards": card_paths,
            "version": target_version,
            "available_artifacts": available_artifacts,
            "missing_artifacts": missing_artifacts,
            "resolved_core_artifacts": resolved_core_artifacts,
            "missing_core_artifacts": missing_core_artifacts,
            "repo_files_indexed": len(repo_files),
        },
    }
