"""Phase-27 deterministic gate tests (consistency_checks + guards firewall).

Real git fixtures under tmp_path, no mocks. Each gate gets a positive case
(defect present → finding) and a negative case (legitimate change → no finding),
per the project's double-path discipline. Each fixture builds a base commit,
then mutates the working tree exactly as a patch would, then runs the gate —
which diffs the working tree against HEAD (== base_commit after the harness's
startup reset).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    StructuralCard,
    SymptomCard,
)
from src.models.patch import FileEditPlan, PatchPlan
from src.orchestrator.consistency_checks import (
    check_config_entry_shape,
    check_contract_drift,
    check_go_unexport_consistency,
    check_parallel_impl_consistency,
    check_removed_symbol_test_refs,
    is_test_file,
    revert_test_file_edits,
)
from src.orchestrator.guards import check_plan_covers_violations

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


# ── fixture helpers ─────────────────────────────────────────────────────────

def _write(repo: Path, rel: str, content: str) -> None:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


def _init(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, msg: str = "base") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


# ── Gate A: contract drift ──────────────────────────────────────────────────

def test_contract_drift_null_to_zero_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "src/user.js",
           "function getExpiry(code) {\n"
           "    return code ? db.pttl(code) : null;\n"
           "}\n")
    _commit(tmp_path)
    # Patch flips the empty-value semantics on the existing else branch.
    _write(tmp_path, "src/user.js",
           "function getExpiry(code) {\n"
           "    return code ? db.pttl(code) : 0;\n"
           "}\n")
    findings = check_contract_drift(tmp_path)
    assert len(findings) == 1
    assert "contract drift" in findings[0].message
    assert findings[0].file == "src/user.js"


def test_contract_drift_new_function_returning_zero_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "src/user.js", "const x = 1;\n")
    _commit(tmp_path)
    # A brand-new function that legitimately returns 0 — no `-` line pairing,
    # so no drift.
    _write(tmp_path, "src/user.js",
           "const x = 1;\n"
           "function count() {\n"
           "    return 0;\n"
           "}\n")
    findings = check_contract_drift(tmp_path)
    assert findings == []


def test_contract_drift_legit_value_change_same_class_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "src/a.py", "def f():\n    return None\n")
    _commit(tmp_path)
    # Change to a non-empty-class expression: not a null<->empty flip.
    _write(tmp_path, "src/a.py", "def f():\n    return compute()\n")
    findings = check_contract_drift(tmp_path)
    assert findings == []


# ── Gate B: parallel-implementation consistency ─────────────────────────────

def test_parallel_impl_missing_guard_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "db/redis.js", "// redis adapter\n")
    _write(tmp_path, "db/mongo.js", "// mongo adapter\n")
    _write(tmp_path, "db/postgres.js", "// postgres adapter\n")
    _commit(tmp_path)
    guarded = (
        "module.mget = async function (keys) {\n"
        "    if (!Array.isArray(keys) || !keys.length) {\n"
        "        return [];\n"
        "    }\n"
        "    return doFetch(keys);\n"
        "}\n"
    )
    unguarded = (
        "module.mget = async function (keys) {\n"
        "    return await client.mget(keys);\n"
        "}\n"
    )
    _write(tmp_path, "db/mongo.js", "// mongo adapter\n" + guarded)
    _write(tmp_path, "db/postgres.js", "// postgres adapter\n" + guarded)
    _write(tmp_path, "db/redis.js", "// redis adapter\n" + unguarded)
    findings = check_parallel_impl_consistency(tmp_path)
    assert len(findings) == 1
    assert findings[0].file == "db/redis.js"
    assert "mget" in findings[0].message


def test_parallel_impl_all_guarded_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "db/redis.js", "// r\n")
    _write(tmp_path, "db/mongo.js", "// m\n")
    _commit(tmp_path)
    guarded = (
        "module.mget = async function (keys) {\n"
        "    if (!keys.length) {\n"
        "        return [];\n"
        "    }\n"
        "    return doFetch(keys);\n"
        "}\n"
    )
    _write(tmp_path, "db/redis.js", "// r\n" + guarded)
    _write(tmp_path, "db/mongo.js", "// m\n" + guarded)
    findings = check_parallel_impl_consistency(tmp_path)
    assert findings == []


# ── Gate C: removed symbol still referenced by tests ────────────────────────

def test_removed_field_referenced_by_test_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "proxy/forwarder.go",
           "package proxy\n\n"
           "type Forwarder struct {\n"
           "\tcfg ForwarderConfig\n"
           "\tclientCredentials *TTLMap\n"
           "}\n")
    _write(tmp_path, "proxy/forwarder_test.go",
           "package proxy\n\n"
           "func TestF(t *testing.T) {\n"
           "\tf := Forwarder{cfg: c, clientCredentials: m}\n"
           "}\n")
    _commit(tmp_path)
    # Patch deletes the clientCredentials field from production code only.
    _write(tmp_path, "proxy/forwarder.go",
           "package proxy\n\n"
           "type Forwarder struct {\n"
           "\tcfg ForwarderConfig\n"
           "}\n")
    findings = check_removed_symbol_test_refs(tmp_path)
    msgs = " ".join(f.message for f in findings)
    assert "clientCredentials" in msgs
    assert any("forwarder_test.go" in f.message for f in findings)


def test_removed_field_no_test_ref_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "proxy/forwarder.go",
           "package proxy\n\n"
           "type Forwarder struct {\n"
           "\tcfg ForwarderConfig\n"
           "\tunusedField *TTLMap\n"
           "}\n")
    _write(tmp_path, "proxy/forwarder_test.go",
           "package proxy\n\n"
           "func TestF(t *testing.T) {\n"
           "\tf := Forwarder{cfg: c}\n"
           "}\n")
    _commit(tmp_path)
    _write(tmp_path, "proxy/forwarder.go",
           "package proxy\n\n"
           "type Forwarder struct {\n"
           "\tcfg ForwarderConfig\n"
           "}\n")
    findings = check_removed_symbol_test_refs(tmp_path)
    assert all("unusedField" not in f.message for f in findings)


# ── Gate D: Go unexport consistency ─────────────────────────────────────────

def test_go_unexport_leftover_method_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "agents/lastfm/client.go",
           "package lastfm\n\n"
           "type Client struct {\n\tbaseURL string\n}\n\n"
           "func NewClient(u string) *Client { return &Client{u} }\n\n"
           "func (c *Client) ValidateToken(k string) error { return nil }\n")
    _commit(tmp_path)
    # Patch lowercases the type but leaves the method + constructor exported.
    _write(tmp_path, "agents/lastfm/client.go",
           "package lastfm\n\n"
           "type client struct {\n\tbaseURL string\n}\n\n"
           "func NewClient(u string) *client { return &client{u} }\n\n"
           "func (c *client) ValidateToken(k string) error { return nil }\n")
    findings = check_go_unexport_consistency(tmp_path)
    msgs = " ".join(f.message for f in findings)
    assert "ValidateToken" in msgs
    assert "NewClient" in msgs


def test_go_unexport_fully_lowercased_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "agents/lastfm/client.go",
           "package lastfm\n\n"
           "type Client struct {\n\tbaseURL string\n}\n\n"
           "func NewClient(u string) *Client { return &Client{u} }\n\n"
           "func (c *Client) ValidateToken(k string) error { return nil }\n")
    _commit(tmp_path)
    _write(tmp_path, "agents/lastfm/client.go",
           "package lastfm\n\n"
           "type client struct {\n\tbaseURL string\n}\n\n"
           "func newClient(u string) *client { return &client{u} }\n\n"
           "func (c *client) validateToken(k string) error { return nil }\n")
    findings = check_go_unexport_consistency(tmp_path)
    assert findings == []


# ── Gate E: config entry shape ──────────────────────────────────────────────

def test_config_entry_extra_field_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "config/data.yml",
           "changelog:\n"
           "  valid_values:\n"
           "    - never\n"
           "    - always\n")
    _commit(tmp_path)
    # Sibling entries are bare scalars; the new entry is a 2-key mapping.
    _write(tmp_path, "config/data.yml",
           "changelog:\n"
           "  valid_values:\n"
           "    - never\n"
           "    - always\n"
           "    - name: patch\n"
           "      desc: Show after patch upgrade\n")
    findings = check_config_entry_shape(tmp_path)
    assert len(findings) >= 1
    assert "valid_values" in findings[0].message


def test_config_entry_matching_shape_not_flagged(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "config/data.yml",
           "opts:\n"
           "  items:\n"
           "    - name: a\n"
           "    - name: b\n")
    _commit(tmp_path)
    # New entry copies the single-key {name} shape of its siblings.
    _write(tmp_path, "config/data.yml",
           "opts:\n"
           "  items:\n"
           "    - name: a\n"
           "    - name: b\n"
           "    - name: c\n")
    findings = check_config_entry_shape(tmp_path)
    assert findings == []


# ── Test-file revert ────────────────────────────────────────────────────────

def test_is_test_file_matrix():
    assert is_test_file("tests/unit/test_x.py")
    assert is_test_file("foo/bar_test.go")
    assert is_test_file("a/b.test.js")
    assert is_test_file("a/b.spec.ts")
    assert is_test_file("test/helpers.py")
    assert not is_test_file("src/user.js")
    assert not is_test_file("lib/forwarder.go")


def test_revert_reverts_tracked_test_edit_and_keeps_prod(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "src/log.py", "def hide():\n    return 1\n")
    _write(tmp_path, "tests/test_log.py", "def test_hide():\n    assert hide()\n")
    _commit(tmp_path)
    # Patch edits BOTH a production file and a test file.
    _write(tmp_path, "src/log.py", "def hide():\n    return 2\n")
    _write(tmp_path, "tests/test_log.py", "def test_hide():\n    assert False\n")
    reverted = revert_test_file_edits(tmp_path)
    assert "tests/test_log.py" in reverted
    # Test file restored to base, production edit preserved.
    assert (tmp_path / "tests/test_log.py").read_text(encoding="utf-8") == \
        "def test_hide():\n    assert hide()\n"
    assert (tmp_path / "src/log.py").read_text(encoding="utf-8") == \
        "def hide():\n    return 2\n"


def test_revert_removes_new_untracked_test_file(tmp_path: Path):
    _init(tmp_path)
    _write(tmp_path, "src/log.py", "x = 1\n")
    _commit(tmp_path)
    # Patch creates a brand-new test file (issue 002 shape).
    _write(tmp_path, "tests/test_qtlog.py", "class TestHideQtWarning: pass\n")
    reverted = revert_test_file_edits(tmp_path)
    assert "tests/test_qtlog.py" in reverted
    assert not (tmp_path / "tests/test_qtlog.py").exists()


# ── Spec-priority firewall: check_plan_covers_violations ────────────────────

def _evidence_with_violated_req(cited: list[str]) -> EvidenceCards:
    return EvidenceCards(
        schema_version="v2",
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=[
            RequirementItem(
                id="req-005",
                text="The last assignment MUST take precedence.",
                origin="requirements",
                verdict="AS_IS_VIOLATED",
                evidence_locations=cited,
                findings="...",
            )
        ],
    )


def test_plan_coverage_gap_flagged():
    ev = _evidence_with_violated_req(["openlibrary/utils.py:291-293"])
    plan = PatchPlan(
        overview="x",
        edits=[FileEditPlan(filepath="openlibrary/lists.py",
                            change_rationale="r")],
    )
    uncovered = check_plan_covers_violations(ev, plan)
    assert uncovered == ["req-005"]


def test_plan_coverage_satisfied_not_flagged():
    ev = _evidence_with_violated_req(["openlibrary/utils.py:291-293"])
    plan = PatchPlan(
        overview="x",
        edits=[FileEditPlan(filepath="openlibrary/utils.py",
                            change_rationale="r")],
    )
    uncovered = check_plan_covers_violations(ev, plan)
    assert uncovered == []


def test_plan_coverage_compliant_req_ignored():
    ev = EvidenceCards(
        schema_version="v2",
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=[
            RequirementItem(
                id="req-001",
                text="X is fine.",
                origin="requirements",
                verdict="AS_IS_COMPLIANT",
                evidence_locations=["a/b.py:1-2"],
                findings="",
            )
        ],
    )
    plan = PatchPlan(
        overview="x",
        edits=[FileEditPlan(filepath="other.py", change_rationale="r")],
    )
    assert check_plan_covers_violations(ev, plan) == []


# ── Go build-error signature enrichment (issue 013) ─────────────────────────

def test_enrich_go_errors_attaches_definition():
    from src.orchestrator.build_verify import BuildError
    from src.orchestrator.engine import _enrich_go_errors_with_definitions

    repo = Path(__import__("tempfile").mkdtemp())
    try:
        _init(repo)
        _write(repo, "lib/cfg.go",
               "package lib\n\n"
               "func UpdateAuthPreference(cc Config) error {\n"
               "\treturn nil\n"
               "}\n")
        _commit(repo)
        errs = [BuildError(
            file="lib/cache.go", line=1106,
            message="assignment mismatch: 2 variables but "
                    "UpdateAuthPreference returns 1 values",
            raw="lib/cache.go:1106",
        )]
        block = _enrich_go_errors_with_definitions(repo, errs)
        assert "UpdateAuthPreference" in block
        assert "func UpdateAuthPreference" in block
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_enrich_go_errors_ignores_non_signature_errors():
    from src.orchestrator.build_verify import BuildError
    from src.orchestrator.engine import _enrich_go_errors_with_definitions

    repo = Path(__import__("tempfile").mkdtemp())
    try:
        _init(repo)
        _write(repo, "lib/cfg.go", "package lib\n")
        _commit(repo)
        errs = [BuildError(
            file="lib/x.go", line=3,
            message="syntax error: unexpected }",
            raw="lib/x.go:3",
        )]
        # No signature-shape keyword → nothing to enrich.
        assert _enrich_go_errors_with_definitions(repo, errs) == ""
    finally:
        shutil.rmtree(repo, ignore_errors=True)

