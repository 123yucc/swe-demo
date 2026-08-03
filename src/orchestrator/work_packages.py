"""Deterministic anchor graph and adaptive deep-search work packages."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from src.models.context import EvidenceCards
from src.models.evidence import RequirementItem
from src.models.report import AdaptiveDeepSearchReport


@dataclass
class AnchorIndex:
    requirement_anchors: dict[str, set[str]] = field(default_factory=dict)

    def add(self, requirement_id: str, anchors: list[str] | set[str]) -> None:
        self.requirement_anchors.setdefault(requirement_id, set()).update(anchors)

    def discover(self, requirement_ids: list[str], anchors: list[str]) -> None:
        """Write newly found files/symbols/call-chain nodes into the graph."""
        for requirement_id in requirement_ids:
            self.add(requirement_id, anchors)


@dataclass
class WorkPackage:
    requirement_ids: list[str]
    anchors: list[str]
    parent_contract_id: str = ""


def build_anchor_index(evidence: EvidenceCards, repo_dir: str | Path | None = None) -> AnchorIndex:
    index = AnchorIndex()
    root = Path(repo_dir).resolve() if repo_dir else None
    rg_path = shutil.which("rg") if root and root.is_dir() else None
    for req in evidence.requirements:
        # Only concrete files are strong enough to batch requirements. Symbols
        # such as "Function" or "Struct" are common parser outputs and create
        # large, unrelated packages unless repository search resolves them to
        # actual files first.
        anchors = set(req.explicit_paths)
        # Deterministic rg only seeds candidates; it never asserts a call chain.
        if root and root.is_dir() and rg_path:
            for symbol in req.explicit_symbols:
                if not re.fullmatch(r"[A-Za-z_$][\w.$-]*", symbol):
                    continue
                try:
                    proc = subprocess.run(
                        [rg_path, "-l", "--fixed-strings", "--", symbol, "."],
                        cwd=root, text=True, capture_output=True, timeout=10,
                        check=False,
                    )
                except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                    # Adaptive batching is an optimization; missing host tools
                    # must degrade to parser-owned explicit paths.
                    break
                anchors.update(line.strip().replace("\\", "/").lstrip("./")
                               for line in proc.stdout.splitlines() if line.strip())
        index.add(req.id, anchors)
    return index


def create_work_packages(
    requirements: list[RequirementItem],
    index: AnchorIndex,
    *,
    max_requirements: int = 4,
) -> list[WorkPackage]:
    """Group by parent contract and overlapping explicit/discovered anchors."""
    max_requirements = max(1, max_requirements)
    packages: list[WorkPackage] = []
    for req in requirements:
        req_anchors = index.requirement_anchors.get(req.id, set())
        primary = next((p for p in packages if len(p.requirement_ids) < max_requirements and (
            (req.parent_contract_id and p.parent_contract_id == req.parent_contract_id)
            or bool(set(p.anchors) & req_anchors)
        )), None)
        if primary is None:
            packages.append(WorkPackage([req.id], sorted(req_anchors), req.parent_contract_id))
            continue
        primary.requirement_ids.append(req.id)
        primary.anchors = sorted(set(primary.anchors) | req_anchors)
    return packages


def validate_work_package_report(package: WorkPackage, report: AdaptiveDeepSearchReport) -> None:
    allowed = set(package.requirement_ids)
    completed = [result.requirement_id for result in report.requirement_results]
    unresolved = report.unresolved_requirement_ids
    if len(completed) != len(set(completed)) or len(unresolved) != len(set(unresolved)):
        raise ValueError("work-package report contains duplicate requirement ids")
    if (set(completed) | set(unresolved)) != allowed:
        raise ValueError("work-package report must account for every requested requirement")
    if set(completed) & set(unresolved):
        raise ValueError("a requirement cannot be both completed and unresolved")
    for result in report.requirement_results:
        if result.scoped_report.target_requirement_id != result.requirement_id:
            raise ValueError("scoped report id does not match requirement result")
        if result.verdict != "TO_BE_MISSING" and not result.evidence_locations:
            raise ValueError("completed requirement lacks independently attributable evidence")
