"""Tests for repatch-round prompt-bloat mitigations (issue 010).

Two independent fixes, both no-mock / direct-call:
  1. `_prune_plan_to_error_files` — on a repatch round keep only the prior
     plan's edits implicated by the build errors, so the full plan is not
     re-inlined into the planner prompt (the bloat that crashed issue 010).
  2. `run_structured_query(allow_none=True)` — returns None instead of raising
     on the empty-structured_output path, letting the planner degrade to
     BUILD_FAILED rather than crashing the run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.orchestrator.build_verify import BuildError
from src.orchestrator.artifact_verify import ArtifactFinding
from src.orchestrator.engine import (
    _augment_repair_plan_with_errors,
    _artifact_findings_to_errors,
    _compact_rationale_themes,
    _compose_change_rationale,
    _expand_config_symbol_owner_context,
    _expand_go_cross_package_owner_context,
    _expand_go_same_package_repair_context,
    _enrich_go_errors_with_package_exports,
    _enrich_go_errors_with_module_import_paths,
    _enrich_removed_symbol_errors_with_base_definitions,
    _merge_patch_plans,
    _prune_plan_to_error_files,
    _reroute_test_compile_errors_to_production_files,
    _verify_plan_coverage,
)
from src.models.patch import PatchPlan, FileEditPlan


def _plan(*paths: str) -> PatchPlan:
    return PatchPlan(
        overview="x",
        edits=[
            FileEditPlan(filepath=p, change_rationale="r")
            for p in paths
        ],
    )


def _err(file: str) -> BuildError:
    return BuildError(file=file, line=1, message="boom", raw="boom")


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True)


def _init_git(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def test_prune_keeps_only_error_files():
    plan = _plan("a/x.go", "a/y.go", "a/z.go")
    errors = [_err("a/y.go")]
    pruned, dropped = _prune_plan_to_error_files(plan, errors)
    assert dropped == 2
    assert pruned is not None
    assert [e.filepath for e in pruned.edits] == ["a/y.go"]
    # Overview is preserved so the planner keeps the strategic context.
    assert pruned.overview == "x"


def test_prune_normalizes_paths():
    # Plan uses "./a/x.go", error uses "a/x.go" — they must match.
    plan = _plan("./a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("a/x.go")])
    assert dropped == 1
    assert [e.filepath for e in pruned.edits] == ["./a/x.go"]


def test_prune_returns_none_when_no_edit_matches():
    # All errors are in test files the plan never touched → nothing to keep.
    plan = _plan("a/x.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("a/x_test.go")])
    assert pruned is None
    assert dropped == 1


def test_prune_keeps_full_plan_when_all_files_error():
    plan = _plan("a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(
        plan, [_err("a/x.go"), _err("a/y.go")]
    )
    assert dropped == 0
    # Same object returned (no needless copy) when nothing is dropped.
    assert pruned is plan


def test_prune_synthetic_build_error_keeps_plan():
    # An un-attributable failure surfaces as file="(build)"; we cannot tell
    # which edits are implicated, so the plan is kept intact rather than
    # wrongly emptied.
    plan = _plan("a/x.go", "a/y.go")
    pruned, dropped = _prune_plan_to_error_files(plan, [_err("(build)")])
    assert pruned is plan
    assert dropped == 0


def test_prune_handles_empty_plan():
    assert _prune_plan_to_error_files(None, [_err("a/x.go")]) == (None, 0)
    empty = PatchPlan(overview="x", edits=[])
    assert _prune_plan_to_error_files(empty, [_err("a/x.go")]) == (empty, 0)


def test_verify_plan_coverage_ignores_test_files() -> None:
    diff = "diff --git a/src/app.go b/src/app.go\n"
    missing = _verify_plan_coverage(diff, ["src/app.go", "server/app_test.go"])
    assert missing == []


def test_merge_patch_plans_preserves_prior_required_files_and_symbols() -> None:
    base = PatchPlan(
        overview="base",
        edits=[
            FileEditPlan(
                filepath="ui/src/utils/index.js",
                change_rationale="export helper",
                expected_symbols=["getClientUniqueId"],
                required_by_requirement_ids=["req-001"],
            ),
            FileEditPlan(
                filepath="ui/src/utils/getClientUniqueId.js",
                change_rationale="create helper module",
                creates_new_file=True,
                required_by_requirement_ids=["req-001"],
            ),
        ],
    )
    repair = PatchPlan(
        overview="repair",
        edits=[
            FileEditPlan(
                filepath="server/server.go",
                change_rationale="repair compile",
                expected_symbols=["ClientUniqueID"],
                required_by_requirement_ids=["req-014"],
            )
        ],
    )

    merged = _merge_patch_plans(base, repair)

    assert merged is not None
    assert [edit.filepath for edit in merged.edits] == [
        "ui/src/utils/index.js",
        "ui/src/utils/getClientUniqueId.js",
        "server/server.go",
    ]
    assert merged.edits[0].expected_symbols == ["getClientUniqueId"]
    assert merged.edits[1].creates_new_file is True
    assert merged.edits[2].required_by_requirement_ids == ["req-014"]


def test_merge_patch_plans_resplits_oversized_same_file_edits() -> None:
    base = PatchPlan(
        overview="base",
        edits=[
            FileEditPlan(
                filepath="internal/cmd/grpc.go",
                target_functions=["NewGRPCServer"],
                change_rationale="planner theme a",
                preserved_findings=[f"constraint {i}" for i in range(8)],
            ),
            FileEditPlan(
                filepath="internal/cmd/grpc.go",
                target_functions=[],
                change_rationale="planner theme b",
                preserved_findings=[f"constraint {i}" for i in range(8, 16)],
            ),
        ],
    )
    extra = PatchPlan(
        overview="repair",
        edits=[
            FileEditPlan(
                filepath="internal/cmd/grpc.go",
                target_functions=[],
                change_rationale="repair theme",
                preserved_findings=[f"constraint {i}" for i in range(16, 25)],
            )
        ],
    )

    merged = _merge_patch_plans(base, extra)

    assert merged is not None
    assert len(merged.edits) == 3
    assert all(edit.filepath == "internal/cmd/grpc.go" for edit in merged.edits)
    assert [len(edit.preserved_findings) for edit in merged.edits] == [12, 12, 1]


def test_compact_rationale_themes_strips_nested_boilerplate_and_bounds_size() -> None:
    themes = _compact_rationale_themes([
        "Direct Stage2 repair for compile gate.\nOriginal planner themes:\n- alpha\n- beta",
        "Direct Stage2 repair for compile gate.\nOriginal planner themes:\n- alpha\n- beta",
        "x" * 1000,
    ])

    assert len(themes) == 2
    assert themes[0] == "Direct Stage2 repair for compile gate."
    assert themes[1].endswith("...")
    assert len(themes[1]) <= 280


def test_compose_change_rationale_bounds_recursive_growth() -> None:
    huge = "Direct Stage2 repair for compile gate.\nOriginal planner themes:\n- " + ("theme " * 5000)

    rationale = _compose_change_rationale(
        header="Merged repair themes for this file.",
        rationales=[huge, huge, "secondary theme"],
    )

    assert rationale.startswith("Merged repair themes for this file.")
    assert "secondary theme" in rationale
    assert len(rationale) < 3000


def test_repair_plan_from_aggregate_keeps_prior_expected_symbols() -> None:
    aggregate = PatchPlan(
        overview="base",
        edits=[
            FileEditPlan(
                filepath="server/middlewares.go",
                change_rationale="preserve logger middleware contract",
                expected_symbols=["injectLogger"],
                required_by_requirement_ids=["req-002"],
            ),
            FileEditPlan(
                filepath="ui/src/utils/index.js",
                change_rationale="export helper",
                expected_symbols=["getClientUniqueId"],
                required_by_requirement_ids=["req-001"],
            ),
        ],
    )
    narrowed = PatchPlan(
        overview="repair",
        edits=[
            FileEditPlan(
                filepath="server/middlewares.go",
                change_rationale="compile repair only",
            )
        ],
    )

    repair_context = _merge_patch_plans(aggregate, narrowed)
    pruned, dropped = _prune_plan_to_error_files(
        repair_context,
        [BuildError(file="server/middlewares.go", line=58, message="undefined: Foo", raw="")],
    )
    augmented = _augment_repair_plan_with_errors(
        pruned,
        [BuildError(file="server/middlewares.go", line=58, message="undefined: Foo", raw="")],
        reason="focused compile gate",
    )

    assert dropped == 1
    assert augmented is not None
    assert augmented.edits[0].filepath == "server/middlewares.go"
    assert augmented.edits[0].expected_symbols == ["injectLogger"]
    assert augmented.edits[0].required_by_requirement_ids == ["req-002"]


def test_augment_repair_plan_adds_error_findings_to_matching_edit():
    plan = _plan("a/x.go", "a/y.go")
    augmented = _augment_repair_plan_with_errors(
        plan,
        [BuildError(file="a/y.go", line=12, message="undefined: Foo", raw="")],
        reason="focused compile gate",
    )

    assert augmented is not None
    y_edit = [e for e in augmented.edits if e.filepath == "a/y.go"][0]
    assert any("focused compile gate: a/y.go:12: undefined: Foo" in f for f in y_edit.preserved_findings)
    x_edit = [e for e in augmented.edits if e.filepath == "a/x.go"][0]
    assert x_edit.preserved_findings == []


def test_augment_repair_plan_synthesizes_missing_error_file_edit():
    augmented = _augment_repair_plan_with_errors(
        _plan("a/x.go"),
        [BuildError(file="a/z.go", line=None, message="removed symbol still referenced", raw="")],
        reason="static patch-closure gate",
    )

    assert augmented is not None
    assert [e.filepath for e in augmented.edits] == ["a/x.go", "a/z.go"]
    synthetic = augmented.edits[-1]
    assert "Direct Stage2 repair" in synthetic.change_rationale
    assert synthetic.preserved_findings == [
        "static patch-closure gate: a/z.go: removed symbol still referenced"
    ]


def test_augment_repair_plan_keeps_heavy_same_file_repair_coherent() -> None:
    plan = PatchPlan(
        overview="base",
        edits=[
            FileEditPlan(
                filepath="cmd/flipt/main.go",
                target_functions=["run"],
                change_rationale="planner theme",
                preserved_findings=[f"constraint {i}" for i in range(10)],
            )
        ],
    )

    augmented = _augment_repair_plan_with_errors(
        plan,
        [
            BuildError(
                file="cmd/flipt/main.go",
                line=120,
                message=f"blocking error {i}",
                raw="",
            )
            for i in range(10)
        ],
        reason="focused compile gate",
    )

    assert augmented is not None
    assert len(augmented.edits) == 1
    assert all(edit.target_functions == ["run"] for edit in augmented.edits)
    assert [len(edit.preserved_findings) for edit in augmented.edits] == [20]


def test_augment_repair_plan_does_not_blow_up_change_rationale() -> None:
    huge = "Direct Stage2 repair for compile gate.\nOriginal planner themes:\n- " + ("theme " * 5000)
    plan = PatchPlan(
        overview="base",
        edits=[
            FileEditPlan(
                filepath="server/events/sse.go",
                target_functions=["SendMessage", "prepareMessage", "ServeHTTP"],
                change_rationale=huge,
                preserved_findings=[f"constraint {i}" for i in range(4)],
            )
        ],
    )

    augmented = _augment_repair_plan_with_errors(
        plan,
        [BuildError(file="server/events/sse.go", line=96, message="assignment mismatch", raw="")],
        reason="focused compile gate",
    )

    assert augmented is not None
    assert len(augmented.edits) == 1
    assert len(augmented.edits[0].change_rationale) < 3000


def test_reroute_test_compile_errors_targets_same_directory_production_files():
    plan = _plan("server/events/diode.go", "server/events/sse.go", "server/server.go")
    errors = [
        BuildError(
            file="server/events/diode_test.go",
            line=24,
            message="diode.set undefined (type *diode has no field or method set)",
            raw="raw",
        )
    ]

    routed = _reroute_test_compile_errors_to_production_files(plan, errors)

    assert [e.file for e in routed] == [
        "server/events/diode.go",
        "server/events/sse.go",
    ]
    assert all("server/events/diode_test.go:24" in e.message for e in routed)


def test_reroute_test_compile_errors_leaves_non_test_errors_untouched():
    plan = _plan("server/events/diode.go")
    errors = [BuildError(file="server/events/sse.go", line=95, message="boom", raw="raw")]

    routed = _reroute_test_compile_errors_to_production_files(plan, errors)

    assert routed == errors


def test_expand_go_same_package_repair_context_adds_sibling_files():
    plan = _plan("server/server.go", "server/middlewares.go", "server/events/sse.go")
    errors = [
        BuildError(
            file="server/server.go",
            line=63,
            message="undefined: clientUniqueIDMiddleware",
            raw="raw",
        )
    ]

    expanded = _expand_go_same_package_repair_context(plan, errors)

    assert [(e.file, e.line) for e in expanded] == [
        ("server/server.go", 63),
        ("server/middlewares.go", None),
    ]
    assert "same-package compile repair context from server/server.go:63" in expanded[1].message


def test_expand_go_cross_package_owner_context_adds_imported_provider_file(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    _write(
        tmp_path,
        "internal/cmd/grpc.go",
        "package cmd\n\n"
        "import middlewaregrpc \"example.com/app/internal/server/middleware/grpc\"\n\n"
        "func init() {\n"
        "\t_, _ = middlewaregrpc.AuditUnaryInterceptor(nil)\n"
        "}\n",
    )
    _write(
        tmp_path,
        "internal/server/middleware/grpc/middleware.go",
        "package grpc\n\n"
        "func AuditUnaryInterceptor() {}\n",
    )
    plan = _plan(
        "internal/cmd/grpc.go",
        "internal/server/middleware/grpc/middleware.go",
    )
    errors = [
        BuildError(
            file="internal/cmd/grpc.go",
            line=12,
            message="not enough arguments in call to middlewaregrpc.AuditUnaryInterceptor",
            raw="raw",
        )
    ]

    expanded = _expand_go_cross_package_owner_context(tmp_path, plan, errors)

    assert [(e.file, e.line) for e in expanded] == [
        ("internal/cmd/grpc.go", 12),
        ("internal/server/middleware/grpc/middleware.go", None),
    ]
    assert "import middlewaregrpc=example.com/app/internal/server/middleware/grpc" in expanded[1].message


def test_expand_go_cross_package_owner_context_uses_missing_member_owner_type(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    _write(
        tmp_path,
        "internal/cmd/grpc.go",
        "package cmd\n\n"
        "import config \"example.com/app/internal/config\"\n\n"
        "func init(cfg config.AuditConfig) {\n"
        "\t_ = cfg.LogFile\n"
        "}\n",
    )
    _write(
        tmp_path,
        "internal/config/audit.go",
        "package config\n\n"
        "type AuditConfig struct{}\n",
    )
    plan = _plan("internal/cmd/grpc.go", "internal/config/audit.go")
    errors = [
        BuildError(
            file="internal/cmd/grpc.go",
            line=9,
            message="cfg.LogFile undefined (type config.AuditConfig has no field or method LogFile)",
            raw="raw",
        )
    ]

    expanded = _expand_go_cross_package_owner_context(tmp_path, plan, errors)
    augmented = _augment_repair_plan_with_errors(
        plan,
        expanded,
        reason="focused compile gate",
    )

    assert [e.file for e in expanded] == [
        "internal/cmd/grpc.go",
        "internal/config/audit.go",
    ]
    assert augmented is not None
    config_edit = [e for e in augmented.edits if e.filepath == "internal/config/audit.go"][0]
    assert "LogFile" in config_edit.expected_symbols


def test_cross_package_exact_symbols_attach_to_owner_not_caller(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    _write(
        tmp_path,
        "internal/server/middleware/grpc/middleware.go",
        "package grpc\n\n"
        "import otel \"example.com/app/internal/server/otel\"\n\n"
        "func x() {\n"
        "\t_ = otel.AuditEventName\n"
        "}\n",
    )
    _write(
        tmp_path,
        "internal/server/otel/attributes.go",
        "package otel\n\n"
        "const Existing = \"x\"\n",
    )
    plan = _plan(
        "internal/server/middleware/grpc/middleware.go",
        "internal/server/otel/attributes.go",
    )
    errors = [
        BuildError(
            file="internal/server/middleware/grpc/middleware.go",
            line=7,
            message="undefined: otel.AuditEventName",
            raw="raw",
        )
    ]

    expanded = _expand_go_cross_package_owner_context(tmp_path, plan, errors)
    augmented = _augment_repair_plan_with_errors(
        plan,
        expanded,
        reason="focused compile gate",
    )

    assert augmented is not None
    caller = [
        e for e in augmented.edits
        if e.filepath == "internal/server/middleware/grpc/middleware.go"
    ][0]
    owner = [e for e in augmented.edits if e.filepath == "internal/server/otel/attributes.go"][0]
    assert caller.expected_symbols == []
    assert owner.expected_symbols == ["AuditEventName"]


def test_expand_config_symbol_owner_context_routes_schema_error_to_config_owner(tmp_path: Path):
    _write(
        tmp_path,
        "config/flipt.schema.json",
        '{"properties":{"audit":{"$ref":"#/definitions/AuditConfig"}}}\n',
    )
    _write(
        tmp_path,
        "internal/config/audit.go",
        "package config\n\n"
        "type AuditConfig struct {\n"
        "\tLogFileSink string\n"
        "}\n",
    )
    _write(
        tmp_path,
        "internal/server/audit/audit.go",
        "package audit\n\n"
        "type Sink struct{}\n",
    )
    plan = _plan(
        "config/flipt.schema.json",
        "internal/config/audit.go",
        "internal/server/audit/audit.go",
    )
    errors = [
        BuildError(
            file="config/flipt.schema.json",
            line=None,
            message=(
                "undefined config symbol: 'AuditConfig' is referenced in "
                "config/flipt.schema.json but no matching class/type/func definition "
                "exists in the patched tree."
            ),
            raw="config/flipt.schema.json: AuditConfig",
        ),
        BuildError(
            file="config/flipt.schema.json",
            line=None,
            message=(
                "undefined config symbol: 'LogFileSink' is referenced in "
                "config/flipt.schema.json but no matching class/type/func definition "
                "exists in the patched tree."
            ),
            raw="config/flipt.schema.json: LogFileSink",
        ),
    ]

    expanded = _expand_config_symbol_owner_context(tmp_path, plan, errors)
    augmented = _augment_repair_plan_with_errors(
        _prune_plan_to_error_files(plan, expanded)[0],
        expanded,
        reason="static patch-closure gate",
    )

    assert [e.file for e in expanded] == [
        "config/flipt.schema.json",
        "config/flipt.schema.json",
        "internal/config/audit.go",
        "internal/config/audit.go",
    ]
    assert augmented is not None
    assert [edit.filepath for edit in augmented.edits] == [
        "config/flipt.schema.json",
        "internal/config/audit.go",
    ]
    schema_edit = [e for e in augmented.edits if e.filepath == "config/flipt.schema.json"][0]
    config_edit = [e for e in augmented.edits if e.filepath == "internal/config/audit.go"][0]
    assert schema_edit.expected_symbols == []
    assert "AuditConfig" in config_edit.expected_symbols
    assert "LogFileSink" in config_edit.expected_symbols


def test_augment_repair_plan_merges_same_file_edits_for_direct_repair():
    plan = PatchPlan(
        overview="repair",
        edits=[
            FileEditPlan(
                filepath="pkg/mod.go",
                target_functions=["alpha"],
                change_rationale="theme alpha",
                preserved_findings=["keep alpha"],
                co_edit_dependencies=["pkg/helper.go"],
                expected_symbols=["Alpha"],
                required_by_requirement_ids=["req-001"],
            ),
            FileEditPlan(
                filepath="pkg/mod.go",
                target_functions=["beta"],
                change_rationale="theme beta",
                preserved_findings=["keep beta"],
                co_edit_dependencies=["pkg/other.go"],
                expected_symbols=["Beta"],
                required_by_requirement_ids=["req-002"],
            ),
        ],
    )

    augmented = _augment_repair_plan_with_errors(
        plan,
        [BuildError(file="pkg/mod.go", line=7, message="undefined: Gamma", raw="")],
        reason="focused compile gate",
    )

    assert augmented is not None
    assert len(augmented.edits) == 1
    merged = augmented.edits[0]
    assert merged.filepath == "pkg/mod.go"
    assert merged.target_functions == ["alpha", "beta"]
    assert "theme alpha" in merged.change_rationale
    assert "theme beta" in merged.change_rationale
    assert "keep alpha" in merged.preserved_findings
    assert "keep beta" in merged.preserved_findings
    assert "focused compile gate: pkg/mod.go:7: undefined: Gamma" in merged.preserved_findings
    assert merged.co_edit_dependencies == ["pkg/helper.go", "pkg/other.go"]
    assert merged.expected_symbols == ["Alpha", "Beta"]
    assert merged.required_by_requirement_ids == ["req-001", "req-002"]


def test_augment_repair_plan_extracts_exact_symbols_from_compile_errors():
    augmented = _augment_repair_plan_with_errors(
        _plan("server/middlewares.go"),
        [
            BuildError(
                file="server/middlewares.go",
                line=58,
                message="undefined: clientUniqueIDMiddleware",
                raw="",
            ),
            BuildError(
                file="server/middlewares.go",
                line=61,
                message="unknown field Data in struct literal of type message, but does have data",
                raw="",
            ),
        ],
        reason="focused compile gate",
    )

    assert augmented is not None
    assert augmented.edits[0].expected_symbols == [
        "Data",
    ]


def test_augment_repair_plan_attaches_undefined_symbol_on_synthetic_owner_edit():
    augmented = _augment_repair_plan_with_errors(
        _plan("server/server.go", "server/middlewares.go"),
        [
            BuildError(
                file="server/middlewares.go",
                line=None,
                message="same-package compile repair context from server/server.go:64: undefined: injectRequestLogger",
                raw="",
            )
        ],
        reason="focused compile gate",
    )

    assert augmented is not None
    middlewares = [e for e in augmented.edits if e.filepath == "server/middlewares.go"][0]
    assert middlewares.expected_symbols == ["injectRequestLogger"]


def test_artifact_repair_preserves_original_target_functions():
    plan = PatchPlan(
        overview="repair",
        edits=[
            FileEditPlan(
                filepath="ui/src/eventStream.js",
                target_functions=["getEventStream"],
                change_rationale="wire client id through event stream",
            )
        ],
    )

    augmented = _augment_repair_plan_with_errors(
        plan,
        [
            BuildError(
                file="ui/src/eventStream.js",
                line=None,
                message="patch_plan requires this file to change, but it is absent from patch.diff",
                raw="",
            )
        ],
        reason="patch artifact verification gate",
    )

    assert augmented is not None
    assert augmented.edits[0].target_functions == ["getEventStream"]
    assert "planner's original localization" in augmented.edits[0].change_rationale


def test_augment_repair_plan_adds_hard_compatibility_instruction_for_removed_symbol():
    augmented = _augment_repair_plan_with_errors(
        _plan("pkg/mod.py"),
        [
            BuildError(
                file="pkg/mod.py",
                line=None,
                message=(
                    "removed symbol still referenced by tests: the patch deleted "
                    "a production surface/member named 'OldAPI' from pkg/mod.py, but "
                    "tests/test_mod.py:7 still references it."
                ),
                raw="",
            )
        ],
        reason="static patch-closure gate",
    )

    assert augmented is not None
    edit = augmented.edits[0]
    joined = "\n".join(edit.preserved_findings)
    assert "MANDATORY compatibility repair" in joined
    assert "Restore a production code surface in pkg/mod.py spelled 'OldAPI'" in joined
    assert "alias, re-export, wrapper, or delegating shim" in joined
    assert "do not merely update callers" in joined


def test_augment_repair_plan_mentions_field_member_compatibility_for_removed_symbol():
    augmented = _augment_repair_plan_with_errors(
        _plan("pkg/mod.go"),
        [
            BuildError(
                file="pkg/mod.go",
                line=None,
                message=(
                    "removed symbol still referenced by tests: the patch deleted "
                    "a production surface/member named 'Data' from pkg/mod.go, but "
                    "pkg/mod_test.go:12 still references it."
                ),
                raw="",
            )
        ],
        reason="static patch-closure gate",
    )

    assert augmented is not None
    joined = "\n".join(augmented.edits[0].preserved_findings)
    assert "field/member" in joined
    assert "same syntactic form" in joined
    assert "Do not edit tests" in joined


def test_artifact_import_symbol_error_targets_source_module(tmp_path: Path):
    _write(tmp_path, "pkg/utils.py", "def existing():\n    pass\n")
    errors = _artifact_findings_to_errors(
        tmp_path,
        [
            ArtifactFinding(
                code="IMPORT_SYMBOL_MISSING",
                file="consumer.py",
                symbol="RetryStrategy",
                target="pkg.utils",
                message="Python from-import name(s) do not resolve",
                raw="from pkg.utils import RetryStrategy",
            )
        ],
    )

    assert [e.file for e in errors] == ["pkg/utils.py"]
    assert "RetryStrategy" in errors[0].message


def test_artifact_js_import_symbol_error_targets_importer_and_owner_module(tmp_path: Path):
    _write(tmp_path, "ui/src/utils/index.js", "export const getClientUniqueId = () => 'x'\n")
    errors = _artifact_findings_to_errors(
        tmp_path,
        [
            ArtifactFinding(
                code="IMPORT_SYMBOL_MISSING",
                file="ui/src/eventStream.js",
                symbol="getClientUniqueId",
                target="./utils",
                message="JS/TS named import does not resolve from module ./utils: getClientUniqueId",
                raw="import { baseUrl, getClientUniqueId } from './utils'",
            )
        ],
    )

    assert [e.file for e in errors] == [
        "ui/src/eventStream.js",
        "ui/src/utils/index.js",
    ]
    assert "getClientUniqueId" in errors[1].message


def test_artifact_js_import_symbol_normalizes_parent_segments(tmp_path: Path):
    _write(tmp_path, "ui/src/utils/index.js", "export const getClientUniqueId = () => 'x'\n")
    errors = _artifact_findings_to_errors(
        tmp_path,
        [
            ArtifactFinding(
                code="IMPORT_SYMBOL_MISSING",
                file="ui/src/dataProvider/httpClient.js",
                symbol="getClientUniqueId",
                target="../utils",
                message="JS/TS named import does not resolve from module ../utils: getClientUniqueId",
                raw="import { getClientUniqueId } from '../utils'",
            )
        ],
    )

    assert [e.file for e in errors] == [
        "ui/src/dataProvider/httpClient.js",
        "ui/src/utils/index.js",
    ]


def test_artifact_missing_js_import_targets_missing_module_file(tmp_path: Path):
    _write(tmp_path, "src/utils/index.js", "export * from './getClientUniqueId'\n")
    errors = _artifact_findings_to_errors(
        tmp_path,
        [
            ArtifactFinding(
                code="IMPORT_TARGET_MISSING",
                file="src/utils/index.js",
                target="./getClientUniqueId",
                message="relative JS/TS import target does not resolve: ./getClientUniqueId",
                raw="export * from './getClientUniqueId'",
            )
        ],
    )

    files = [e.file for e in errors]
    assert "src/utils/index.js" in files
    assert "src/utils/getClientUniqueId.js" in files
    assert any("create/export the module at src/utils/getClientUniqueId.js" in e.message for e in errors)


def test_removed_symbol_enrichment_includes_base_python_definition(tmp_path: Path):
    _init_git(tmp_path)
    _write(
        tmp_path,
        "pkg/mod.py",
        "@decorator\n"
        "class OldAPI:\n"
        "    def method(self):\n"
        "        return 1\n\n"
        "def other():\n"
        "    return 2\n",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")

    feedback = _enrich_removed_symbol_errors_with_base_definitions(
        tmp_path,
        [
            BuildError(
                file="pkg/mod.py",
                line=None,
                message="removed symbol still referenced by tests: the patch deleted a production surface/member named 'OldAPI' from pkg/mod.py",
                raw="",
            )
        ],
    )

    assert "class OldAPI" in feedback
    assert "def method" in feedback
    assert "def other" not in feedback


def test_go_undefined_enrichment_lists_actual_package_exports(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.20\n")
    _write(
        tmp_path,
        "internal/audit/audit.go",
        "package audit\n\n"
        "type Action string\n"
        "const (\n"
        "    Create Action = \"create\"\n"
        ")\n",
    )
    _write(
        tmp_path,
        "internal/mw/middleware.go",
        "package mw\n\n"
        "import \"example.com/app/internal/audit\"\n\n"
        "func f() { _ = audit.ActionCreate }\n",
    )

    feedback = _enrich_go_errors_with_package_exports(
        tmp_path,
        [
            BuildError(
                file="internal/mw/middleware.go",
                line=5,
                message="undefined: audit.ActionCreate",
                raw="",
            )
        ],
    )

    assert "missing symbol(s): ActionCreate" in feedback
    assert "Action ::" in feedback
    assert "Create ::" in feedback


def test_go_import_path_enrichment_uses_current_module(tmp_path: Path):
    _write(tmp_path, "go.mod", "module go.flipt.io/flipt\n\ngo 1.20\n")
    (tmp_path / "internal/server/audit").mkdir(parents=True)

    feedback = _enrich_go_errors_with_module_import_paths(
        tmp_path,
        [
            BuildError(
                file="internal/server/audit/logfile/logfile.go",
                line=13,
                message="no required module provides package github.com/granted-dev/granted/internal/server/audit; to add it:",
                raw="",
            )
        ],
    )

    assert "go.mod module is go.flipt.io/flipt" in feedback
    assert "go.flipt.io/flipt/internal/server/audit" in feedback
    assert "github.com/granted-dev/granted" not in feedback.split("must use", 1)[1]
