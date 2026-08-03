import asyncio

from src.agents.patch_generator_agent import (
    _build_current_file_prompt_context,
    _build_openai_direct_patch_prompt,
    _build_single_edit_prompt,
    _can_accept_idempotent_noop,
    _classify_attempt,
    _extract_full_file_content,
    _is_infra_failure_signal,
    _is_explicit_patch_format_failure,
    _retry_preamble_for,
    _should_promote_silent_same_file_failure,
    _should_retry_file_wide,
    _should_retry_missing_required_diff,
    _writes_full_file,
)
from src.agents.patch_planner_agent import (
    _annotate_artifact_expectations,
    _backfill_declared_coedit_files,
    _split_heavy_edits,
)
from src.models.context import EvidenceCards
from src.models.evidence import ConstraintCard, LocalizationCard, StructuralCard, SymptomCard
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan
from src.tools import patch_tools
from src.tools.ingestion_tools import init_working_memory, set_repo_root
from src.tools.patch_tools import create_file


def _empty_evidence() -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
    )


def test_extract_full_file_content_from_code_fence():
    assert _extract_full_file_content("```python\nprint('x')\n```") == "print('x')\n"


def test_empty_existing_file_uses_full_file_write(tmp_path):
    target = tmp_path / "pkg" / "__init__.py"
    target.parent.mkdir()
    target.write_text("")
    edit = FileEditPlan(filepath="pkg/__init__.py", change_rationale="export symbols")

    assert _writes_full_file(edit, tmp_path)


def test_missing_planned_file_uses_full_file_write(tmp_path):
    edit = FileEditPlan(
        filepath="internal/server/audit/audit.go",
        change_rationale="add audit package",
        target_functions=["NewEvent"],
    )

    assert _writes_full_file(edit, tmp_path)


def test_existing_created_file_repair_uses_search_replace(tmp_path):
    target = tmp_path / "pkg" / "new_module.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n")
    edit = FileEditPlan(
        filepath="pkg/new_module.py",
        change_rationale="repair generated module",
        target_functions=["VALUE"],
        creates_new_file=True,
    )

    assert not _writes_full_file(edit, tmp_path)


def test_error_already_exists_is_not_idempotent():
    assert _classify_attempt(
        hash_changed=False,
        tool_calls_delta=0,
        result_text="ERROR: File already exists: pkg/new.py",
    ) == "FAILED"


def test_create_file_tool_creates_planned_new_file(tmp_path):
    set_repo_root(tmp_path)
    init_working_memory("issue", _empty_evidence())

    result = asyncio.run(create_file.handler({
        "filepath": "pkg/new_module.py",
        "content": "VALUE = 1\n",
    }))

    assert "Successfully created file pkg/new_module.py" in result["content"][0]["text"]
    assert (tmp_path / "pkg" / "new_module.py").read_text() == "VALUE = 1\n"


def test_create_file_tool_refuses_empty_file(tmp_path):
    set_repo_root(tmp_path)
    init_working_memory("issue", _empty_evidence())

    result = asyncio.run(create_file.handler({
        "filepath": "pkg/empty.py",
        "content": "",
    }))

    assert "Refusing to create empty file" in result["content"][0]["text"]
    assert not (tmp_path / "pkg" / "empty.py").exists()


def test_typescript_syntax_check_does_not_invoke_node(tmp_path, monkeypatch):
    target = tmp_path / "src" / "module.ts"
    target.parent.mkdir()
    target.write_text("export const value: string = 'ok'\n", encoding="utf-8")

    monkeypatch.setattr(
        patch_tools,
        "run_repo_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("TypeScript must not be passed to node --check")
        ),
    )

    assert patch_tools._validate_syntax(target, tmp_path) == ""


def test_javascript_syntax_check_still_uses_node(tmp_path, monkeypatch):
    target = tmp_path / "src" / "module.js"
    target.parent.mkdir()
    target.write_text("export const value = 'ok'\n", encoding="utf-8")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return 0, "", False

    monkeypatch.setattr(patch_tools, "run_repo_command", fake_run)

    assert patch_tools._validate_syntax(target, tmp_path) == ""
    assert observed["command"] == ["node", "--check", "src/module.js"]


def test_infra_failure_signal_detects_relay_outages():
    assert _is_infra_failure_signal(
        "Error code: 500 - {'error': {'type': 'buzz_error', 'code': 'get_channel_failed'}}"
    ) is True
    assert _is_infra_failure_signal("ERROR: SEARCH text not found") is False


def test_openai_direct_patch_prompt_includes_repair_feedback(tmp_path):
    target = tmp_path / "pkg" / "x.go"
    target.parent.mkdir()
    target.write_text("package pkg\n\nfunc Use() {}\n")
    memory = SharedWorkingMemory(
        issue_context="issue",
        evidence_cards=_empty_evidence(),
        custom_repair_block="Go version: do not use predeclared any.",
        build_error_feedback="pkg/x.go:3: predeclared any requires go1.18 or later",
    )
    edit = FileEditPlan(filepath="pkg/x.go", change_rationale="fix compile")

    prompt = _build_openai_direct_patch_prompt(
        memory,
        tmp_path,
        edit,
        sub_edit_label="pkg/x.go edit",
    )

    assert "## Custom Repair Discipline" in prompt
    assert "do not use predeclared any" in prompt
    assert "## Blocking Verification Feedback" in prompt
    assert "predeclared any requires go1.18" in prompt


def test_retry_scope_escalates_to_file_wide_for_search_replace_failures():
    edit = FileEditPlan(
        filepath="ui/src/eventStream.js",
        target_functions=["getEventStream"],
        change_rationale="wire client id through event stream transport",
    )

    assert _should_retry_file_wide(
        edit,
        "ERROR in block 1/1: SEARCH text not found in ui/src/eventStream.js.",
    ) is True
    assert _should_retry_file_wide(
        edit,
        "ERROR: Malformed SEARCH/REPLACE block: missing '======SPLIT' separator.",
    ) is True


def test_retry_scope_does_not_escalate_for_new_file_or_non_scope_failures():
    assert _should_retry_file_wide(
        FileEditPlan(
            filepath="pkg/new_file.py",
            target_functions=["build"],
            creates_new_file=True,
            change_rationale="create new file",
        ),
        "SEARCH text not found",
    ) is False
    assert _should_retry_file_wide(
        FileEditPlan(
            filepath="pkg/x.py",
            target_functions=["build"],
            change_rationale="edit function only",
        ),
        "permission denied",
    ) is False


def test_explicit_patch_format_failure_is_not_treated_as_silent_follow_on():
    assert _is_explicit_patch_format_failure(
        "ERROR: Malformed SEARCH/REPLACE block: missing '======SPLIT' separator."
    ) is True
    assert _should_promote_silent_same_file_failure("empty result") is True
    assert _should_promote_silent_same_file_failure(
        "PATCH_INCOMPLETE: OpenAI response did not contain SEARCH/REPLACE blocks."
    ) is False


def test_file_wide_retry_prompt_allows_same_file_top_level_wiring(tmp_path):
    target = tmp_path / "ui" / "src" / "eventStream.js"
    target.parent.mkdir(parents=True)
    target.write_text("const getEventStream = async () => {}\n", encoding="utf-8")
    memory = SharedWorkingMemory(
        issue_context="issue",
        evidence_cards=_empty_evidence(),
    )
    edit = FileEditPlan(
        filepath="ui/src/eventStream.js",
        change_rationale="fix event stream transport",
    )

    prompt = _build_single_edit_prompt(
        memory,
        tmp_path,
        edit,
        sub_edit_label="ui/src/eventStream.js edit 1/1 (getEventStream)",
        retry_preamble=_retry_preamble_for(
            "ui/src/eventStream.js edit 1/1 (getEventStream)",
            "SEARCH text not found",
            file_wide_retry=True,
        ),
    )

    assert "SAME-FILE file-wide pass" in prompt
    assert "target_functions: (unspecified)" in prompt


def test_single_edit_prompt_marks_expected_symbols_as_hard_constraints(tmp_path):
    target = tmp_path / "server" / "middlewares.go"
    target.parent.mkdir(parents=True)
    target.write_text("package server\n", encoding="utf-8")
    memory = SharedWorkingMemory(
        issue_context="issue",
        evidence_cards=_empty_evidence(),
    )
    edit = FileEditPlan(
        filepath="server/middlewares.go",
        change_rationale="preserve middleware symbols",
        expected_symbols=["clientUniqueId", "requestLoggerContext"],
    )

    prompt = _build_single_edit_prompt(memory, tmp_path, edit)

    assert "expected_symbols: clientUniqueId, requestLoggerContext" in prompt
    assert "Respect expected_symbols as hard constraints" in prompt


def test_required_diff_retry_only_applies_to_non_reference_required_edits():
    required = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="repair export",
    )
    assert _should_retry_missing_required_diff(required, ["ui/src/utils/index.js"]) is True

    reference_only = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="read only",
        reference_only=True,
    )
    assert _should_retry_missing_required_diff(reference_only, ["ui/src/utils/index.js"]) is False

    optional = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="optional",
        expected_diff_required=False,
    )
    assert _should_retry_missing_required_diff(optional, ["ui/src/utils/index.js"]) is False


def test_idempotent_noop_acceptance_only_applies_to_reference_or_optional_edits():
    required = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="repair export",
    )
    assert _can_accept_idempotent_noop(required, ["ui/src/utils/index.js"]) is False

    reference_only = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="read only",
        reference_only=True,
    )
    assert _can_accept_idempotent_noop(reference_only, ["ui/src/utils/index.js"]) is True

    optional = FileEditPlan(
        filepath="ui/src/utils/index.js",
        change_rationale="optional",
        expected_diff_required=False,
    )
    assert _can_accept_idempotent_noop(optional, ["ui/src/utils/index.js"]) is True


def test_patch_planner_cleans_placeholder_expected_symbols(tmp_path):
    memory = SharedWorkingMemory(
        issue_context="issue",
        evidence_cards=_empty_evidence(),
    )
    plan = PatchPlan(
        overview="plan",
        edits=[
            FileEditPlan(
                filepath="ui/src/utils/index.js",
                change_rationale="export helper",
                expected_symbols=["(exported client unique id helper)", "(*diode).put"],
            )
        ],
    )

    _annotate_artifact_expectations(plan, memory, tmp_path)

    assert plan.edits[0].expected_symbols == ["put"]


def test_patch_planner_splits_oversized_function_edit():
    plan = PatchPlan(
        overview="plan",
        edits=[
            FileEditPlan(
                filepath="internal/server/audit/audit.go",
                target_functions=[
                    "Event.DecodeToAttributes",
                    "SinkSpanExporter.ExportSpans",
                    "SinkSpanExporter.Shutdown",
                    "NewEvent",
                ],
                change_rationale="implement audit event pipeline",
                preserved_findings=[
                    "Event.DecodeToAttributes must emit `flipt.event.version`.",
                    "SinkSpanExporter.ExportSpans must ignore non-conforming events.",
                    "SinkSpanExporter.Shutdown must close all sinks cleanly.",
                    "NewEvent must set version metadata.",
                    "The log-file sink should append one JSON object per line.",
                    "Identity metadata should be included when available.",
                ],
                expected_symbols=["Event", "SinkSpanExporter", "NewEvent"],
            )
        ],
    )

    _split_heavy_edits(plan)

    assert len(plan.edits) == 2
    assert plan.edits[0].target_functions == [
        "Event.DecodeToAttributes",
        "SinkSpanExporter.ExportSpans",
        "SinkSpanExporter.Shutdown",
    ]
    assert "Event.DecodeToAttributes must emit `flipt.event.version`." in plan.edits[0].preserved_findings
    assert "SinkSpanExporter.ExportSpans must ignore non-conforming events." in plan.edits[0].preserved_findings
    assert "SinkSpanExporter.Shutdown must close all sinks cleanly." in plan.edits[0].preserved_findings
    assert plan.edits[1].target_functions == ["NewEvent"]
    assert "NewEvent must set version metadata." in plan.edits[1].preserved_findings
    combined = {
        finding
        for edit in plan.edits
        for finding in edit.preserved_findings
    }
    assert "The log-file sink should append one JSON object per line." in combined
    assert "Identity metadata should be included when available." in combined


def test_patch_planner_splits_targeted_edit_by_findings_limit():
    plan = PatchPlan(
        overview="plan",
        edits=[
            FileEditPlan(
                filepath="cmd/flipt/main.go",
                target_functions=["run"],
                change_rationale="repair shutdown sequencing",
                preserved_findings=[f"constraint {i}" for i in range(25)],
            )
        ],
    )

    _split_heavy_edits(plan)

    assert len(plan.edits) == 3
    assert all(edit.target_functions == ["run"] for edit in plan.edits)
    assert [len(edit.preserved_findings) for edit in plan.edits] == [12, 12, 1]


def test_build_current_file_prompt_context_uses_excerpts_for_large_targeted_file():
    content = "\n".join(
        [f"line {i}" for i in range(1, 220)]
        + ["", "func run() {", "    doWork()", "}"]
        + [f"tail {i}" for i in range(220, 420)]
    )

    label, body = _build_current_file_prompt_context(content, ["run"])

    assert label == "Current file excerpts"
    assert "Excerpt 1" in body
    assert "func run() {" in body
    assert "tail 419" not in body


def test_patch_planner_splits_filewide_findings_only_edit():
    plan = PatchPlan(
        overview="plan",
        edits=[
            FileEditPlan(
                filepath="config/flipt.schema.json",
                change_rationale="extend schema",
                preserved_findings=[f"constraint {i}" for i in range(14)],
            )
        ],
    )

    _split_heavy_edits(plan)

    assert len(plan.edits) == 2
    assert plan.edits[0].target_functions == []
    assert plan.edits[1].target_functions == []
    assert len(plan.edits[0].preserved_findings) == 12
    assert len(plan.edits[1].preserved_findings) == 2


def test_patch_planner_backfill_skips_test_files():
    memory = SharedWorkingMemory(
        issue_context="issue",
        evidence_cards=EvidenceCards(
            symptom=SymptomCard(),
            constraint=ConstraintCard(),
            localization=LocalizationCard(),
            structural=StructuralCard(
                must_co_edit_relations=[
                    "internal/config/config.go and internal/config/config_test.go must be updated together."
                ]
            ),
        ),
    )
    plan = PatchPlan(
        overview="plan",
        edits=[
            FileEditPlan(
                filepath="internal/config/config.go",
                change_rationale="extend config loader",
            )
        ],
    )

    appended = _backfill_declared_coedit_files(plan, memory)

    assert appended == []
    assert [edit.filepath for edit in plan.edits] == ["internal/config/config.go"]
