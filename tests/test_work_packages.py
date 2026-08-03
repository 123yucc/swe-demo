import subprocess
import shutil

import pytest

from src.models.evidence import RequirementItem
from src.models.report import AdaptiveDeepSearchReport, DeepSearchReport, RequirementInvestigationResult
from src.models.context import EvidenceCards
from src.orchestrator.work_packages import (
    AnchorIndex,
    WorkPackage,
    build_anchor_index,
    create_work_packages,
    validate_work_package_report,
)


def _req(rid, parent, path):
    return RequirementItem(id=rid, text=rid, origin="requirements", parent_contract_id=parent, explicit_paths=[path])


def test_packages_merge_parent_contract_and_shared_anchor():
    reqs = [_req("req-001", "c1", "a.py"), _req("req-002", "c1", "b.py"), _req("req-003", "c2", "a.py")]
    index = AnchorIndex({r.id: set(r.explicit_paths) for r in reqs})
    packages = create_work_packages(reqs, index)
    assert len(packages) == 1
    assert set(packages[0].requirement_ids) == {"req-001", "req-002", "req-003"}


def test_partial_completion_must_account_for_every_id():
    package = WorkPackage(["req-001", "req-002"], ["a.py"])
    scoped = DeepSearchReport(target_requirement_id="req-001", requirement_verdict="AS_IS_VIOLATED", requirement_evidence_locations=["a.py:1"])
    report = AdaptiveDeepSearchReport(
        requirement_results=[
            RequirementInvestigationResult(
                requirement_id="req-001",
                scoped_report=scoped,
            )
        ],
        unresolved_requirement_ids=["req-002"],
    )
    validate_work_package_report(package, report)
    report.unresolved_requirement_ids = []
    with pytest.raises(ValueError, match="account"):
        validate_work_package_report(package, report)


def test_packages_respect_maximum_size():
    reqs = [_req(f"req-{i:03}", "c1", "shared.py") for i in range(1, 7)]
    index = AnchorIndex({r.id: set(r.explicit_paths) for r in reqs})
    packages = create_work_packages(reqs, index, max_requirements=4)
    assert [len(package.requirement_ids) for package in packages] == [4, 2]


def test_anchor_index_ignores_unresolved_generic_symbols(monkeypatch, tmp_path):
    reqs = [
        RequirementItem(
            id=f"req-{i:03}", text="x", origin="requirements",
            explicit_symbols=["Function"],
        )
        for i in range(1, 3)
    ]
    monkeypatch.setattr(
        shutil, "which", lambda name: None,
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: pytest.fail("subprocess should not run without rg"),
    )
    evidence = EvidenceCards.model_construct(requirements=reqs)
    index = build_anchor_index(evidence, tmp_path)
    packages = create_work_packages(reqs, index)
    assert len(packages) == 2
