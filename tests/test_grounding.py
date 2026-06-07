"""Static grounding gate tests (phase 25, gap A/B + ③).

Real file-system fixtures under tmp_path, no mocks — each check gets a positive
(grounded → no failure) and negative (refuted → failure) case.
"""

from __future__ import annotations

from pathlib import Path

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    ScopedEvidence,
    StructuralCard,
    SymptomCard,
)
from src.orchestrator.ast_grounding import (
    build_symbol_index,
    has_call_edge,
    has_exception_class,
    has_symbol_def,
)
from src.orchestrator.grounding import (
    attribute_field_failure_to_req,
    ground_call_chain,
    ground_exact_code_regions,
    ground_findings_snippets,
    ground_missing_elements,
    ground_suspect_entities,
    ground_symptom_symbols,
    run_static_grounding,
)


def _write(repo: Path, rel: str, content: str) -> None:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


_SAMPLE_PY = '''\
class FooError(Exception):
    pass


def helper(x):
    return x + 1


def process(data):
    value = helper(data)
    return value
'''


# ── ground_exact_code_regions ──────────────────────────────────────────────

def test_region_in_bounds_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_exact_code_regions("req-001", ["mod.py:5-6"], tmp_path)
    assert failures == []


def test_region_out_of_bounds_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_exact_code_regions("req-001", ["mod.py:500-600"], tmp_path)
    assert len(failures) == 1
    assert failures[0].kind == "region_oob"
    assert failures[0].requirement_id == "req-001"


def test_region_missing_file_fails(tmp_path: Path):
    failures = ground_exact_code_regions("req-001", ["ghost.py:1-2"], tmp_path)
    assert len(failures) == 1
    assert "not found" in failures[0].detail


# ── ground_suspect_entities ────────────────────────────────────────────────

def test_suspect_symbol_present_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_suspect_entities("req-001", ["mod.py:helper"], tmp_path)
    assert failures == []


def test_suspect_symbol_absent_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_suspect_entities("req-001", ["mod.py:nonexistent_fn"], tmp_path)
    assert len(failures) == 1
    assert failures[0].kind == "symbol_absent"


# ── ground_findings_snippets ───────────────────────────────────────────────

def test_findings_snippet_present_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_findings_snippets(
        "req-001", "The `helper` function adds one.", ["mod.py:5"], tmp_path
    )
    assert failures == []


def test_findings_snippet_absent_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_findings_snippets(
        "req-001", "It calls `db.mget` to fetch.", ["mod.py:5"], tmp_path
    )
    assert len(failures) == 1
    assert failures[0].kind == "finding_snippet_absent"


# ── ground_missing_elements ────────────────────────────────────────────────

def test_missing_element_truly_absent_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_missing_elements(["Need a `brandNewSymbol` function"], tmp_path)
    assert failures == []


def test_missing_element_actually_present_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_missing_elements(["Must add `helper` which is absent"], tmp_path)
    assert len(failures) == 1
    assert failures[0].kind == "missing_element_present"


# ── AST: call chain ────────────────────────────────────────────────────────

def test_call_edge_present_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    # process -> helper edge exists.
    failures = ground_call_chain("req-001", ["process -> helper"], ["mod.py:9"], tmp_path)
    assert failures == []


def test_call_edge_absent_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    # process does NOT call FooError.
    failures = ground_call_chain("req-001", ["process -> FooError"], ["mod.py:9"], tmp_path)
    assert len(failures) == 1
    assert failures[0].kind == "call_edge_absent"


def test_call_chain_unparseable_is_soft_pass(tmp_path: Path):
    # No file to parse → soft pass, no failures.
    failures = ground_call_chain("req-001", ["a -> b"], [], tmp_path)
    assert failures == []


# ── AST: symptom symbols ───────────────────────────────────────────────────

def test_symptom_known_exception_passes(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_symptom_symbols(["raises `FooError` on bad input"], tmp_path)
    assert failures == []


def test_symptom_unknown_symbol_fails(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    failures = ground_symptom_symbols(["raises `TotallyMadeUpError`"], tmp_path)
    assert len(failures) == 1
    assert failures[0].kind == "symptom_symbol_absent"


# ── AST query units ────────────────────────────────────────────────────────

def test_ast_python_index_queries():
    idx = build_symbol_index("mod.py", _SAMPLE_PY)
    assert idx is not None
    assert has_symbol_def(idx, "helper")
    assert has_exception_class(idx, "FooError")
    assert not has_exception_class(idx, "helper")
    assert has_call_edge(idx, "process", "helper")
    assert not has_call_edge(idx, "helper", "process")


def test_ast_unsupported_language_returns_none():
    assert build_symbol_index("data.txt", "whatever") is None


def test_ast_syntax_error_returns_none():
    assert build_symbol_index("mod.py", "def broken(:\n") is None


# ── gap B: attribute_field_failure_to_req ──────────────────────────────────

def _cards(*reqs: RequirementItem, **kw) -> EvidenceCards:
    return EvidenceCards(
        symptom=kw.get("symptom", SymptomCard()),
        constraint=kw.get("constraint", ConstraintCard()),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(reqs),
    )


def test_attribute_by_path():
    req = RequirementItem(
        id="req-001", text="some behavior", origin="requirements",
        verdict="AS_IS_VIOLATED", evidence_locations=["src/app.py:10"],
    )
    rid, matched_by = attribute_field_failure_to_req(
        "missing thing at src/app.py:10 here", _cards(req)
    )
    assert rid == "req-001"
    assert matched_by == "path"


def test_attribute_by_token():
    req = RequirementItem(
        id="req-002",
        text="redis connection pool must close gracefully",
        origin="requirements", verdict="AS_IS_VIOLATED",
        evidence_locations=["x.py:1"],
    )
    rid, matched_by = attribute_field_failure_to_req(
        "redis connection leaks on shutdown", _cards(req)
    )
    assert rid == "req-002"
    assert matched_by == "token"


def test_attribute_falls_back_to_global():
    req = RequirementItem(
        id="req-003", text="unrelated topic xyzzy", origin="requirements",
        verdict="AS_IS_VIOLATED", evidence_locations=["q.py:1"],
    )
    rid, matched_by = attribute_field_failure_to_req(
        "completely different subject foobar", _cards(req)
    )
    assert rid == "<global>"
    assert matched_by == "global"


# ── run_static_grounding end-to-end ────────────────────────────────────────

def test_run_static_grounding_attributes_global_missing_element(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    req = RequirementItem(
        id="req-001",
        text="the helper function behavior",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["mod.py:5"],
        findings="helper adds one",
        scoped_evidence=ScopedEvidence(
            localization=LocalizationCard(suspect_entities=["mod.py:helper"]),
        ),
    )
    ev = _cards(
        req,
        constraint=ConstraintCard(
            missing_elements_to_implement=["Must add `helper` (currently absent)"]
        ),
    )
    failures = run_static_grounding(ev, tmp_path)
    # `helper` actually exists → missing_element_present, attributed by token
    # overlap to req-001 (shares "helper").
    me = [f for f in failures if f.kind == "missing_element_present"]
    assert len(me) == 1
    assert me[0].requirement_id == "req-001"
    assert me[0].matched_by == "scoped"


def test_run_static_grounding_clean_repo_no_failures(tmp_path: Path):
    _write(tmp_path, "mod.py", _SAMPLE_PY)
    req = RequirementItem(
        id="req-001",
        text="helper adds one to input",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["mod.py:5-6"],
        findings="The `helper` function returns x + 1.",
        scoped_evidence=ScopedEvidence(
            localization=LocalizationCard(
                exact_code_regions=["mod.py:5-6"],
                suspect_entities=["mod.py:helper"],
                call_chain_context=["process -> helper"],
            )
        ),
    )
    failures = run_static_grounding(_cards(req), tmp_path)
    assert failures == []
