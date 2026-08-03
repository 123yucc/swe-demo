"""Unit tests for src/orchestrator/build_verify.py parsers and helpers.

Per project policy (CLAUDE.md): no mocks. These feed REAL compiler / pytest
output captured from the issue 008/009/010 evaluation logs into the parsers and
assert the extracted structured errors, plus bidirectional checks (what must be
present AND what must be ignored).

Run:  python -m pytest tests/test_build_verify.py -v
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.orchestrator.build_verify import (
    BuildCheckResult,
    BuildError,
    changed_go_packages,
    changed_python_production_files,
    detect_build_system,
    diff_new_errors,
    parse_go_errors,
    parse_python_errors,
    render_errors_for_feedback,
    run_build_check,
)


def test_changed_go_packages_uses_canonical_host_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True,
    )
    pkg = tmp_path / "scanner"
    pkg.mkdir()
    source = pkg / "parser.go"
    source.write_text("package scanner\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    source.write_text("package scanner\n\nfunc parse() {}\n", encoding="utf-8")

    assert changed_go_packages(tmp_path) == ["./scanner"]


def test_changed_python_production_files_uses_canonical_host_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    source = pkg / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_file = test_dir / "test_module.py"
    test_file.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    test_file.write_text("def test_changed():\n    pass\n", encoding="utf-8")
    new_source = pkg / "new_module.py"
    new_source.write_text("VALUE = 3\n", encoding="utf-8")

    assert changed_python_production_files(tmp_path) == [
        "pkg/module.py",
        "pkg/new_module.py",
    ]

# ── Real captured output (verbatim from eval logs) ──────────────────────────

# issue 009 — gravitational/teleport, workspace/stderr.log
TELEPORT_STDERR = """\
# github.com/gravitational/teleport/lib/service
lib/service/kubernetes.go:234:4: unknown field 'Auth' in struct literal of type proxy.ForwarderConfig
lib/service/kubernetes.go:238:4: unknown field 'AccessPoint' in struct literal of type proxy.ForwarderConfig
lib/service/service.go:2556:5: unknown field 'Tunnel' in struct literal of type proxy.ForwarderConfig
lib/service/service.go:2557:5: unknown field 'Auth' in struct literal of type proxy.ForwarderConfig
# github.com/gravitational/teleport/lib/kube/proxy [github.com/gravitational/teleport/lib/kube/proxy.test]
lib/kube/proxy/forwarder_test.go:357:5: f.cfg undefined (type *Forwarder has no field or method cfg)
lib/kube/proxy/forwarder_test.go:546:3: unknown field 'clientCredentials' in struct literal of type Forwarder
"""

# issue 010 — navidrome/navidrome, workspace/stdout.log
NAVIDROME_STDOUT = """\
# github.com/navidrome/navidrome/core/agents/listenbrainz [github.com/navidrome/navidrome/core/agents/listenbrainz.test]
core/agents/listenbrainz/auth_router_test.go:27:9: undefined: newClient
core/agents/listenbrainz/client_test.go:21:12: undefined: newClient
# github.com/navidrome/navidrome/core/agents/spotify [github.com/navidrome/navidrome/core/agents/spotify.test]
core/agents/spotify/client_test.go:20:12: undefined: newClient
FAIL\tgithub.com/navidrome/navidrome/core/agents/lastfm [build failed]
FAIL\tgithub.com/navidrome/navidrome/core/agents/listenbrainz [build failed]
"""

# issue 008 — qutebrowser/qutebrowser, workspace/stdout.log
QUTEBROWSER_STDOUT = """\
qutebrowser/config/configdata.py:112: in _parse_yaml_type
    typ = getattr(configtypes, type_name)
E   AttributeError: module 'qutebrowser.config.configtypes' has no attribute 'ChangelogAfterUpgrade'

During handling of the above exception, another exception occurred:
qutebrowser/config/configdata.py:114: in _parse_yaml_type
    raise AttributeError("Did not find type {} for {}".format(
E   AttributeError: Did not find type ChangelogAfterUpgrade for changelog_after_upgrade
=========================== short test summary info ============================
ERROR tests/unit/config/test_configfiles.py::test_state_config[None-False-foo]
ERROR tests/unit/config/test_configfiles.py::TestConfigPy::test_init
"""


# ── Go parser ───────────────────────────────────────────────────────────────

def test_parse_go_errors_teleport_production_and_test_files():
    errors = parse_go_errors(TELEPORT_STDERR)
    files = {e.file for e in errors}
    # Production files (caught by `go build`) AND test file (caught by `go vet`).
    assert "lib/service/kubernetes.go" in files
    assert "lib/service/service.go" in files
    assert "lib/kube/proxy/forwarder_test.go" in files
    # A specific error is faithfully captured (file, line, message).
    auth_err = next(
        e for e in errors
        if e.file == "lib/service/service.go" and e.line == 2557
    )
    assert "unknown field 'Auth'" in auth_err.message


def test_parse_go_errors_ignores_package_headers_and_fail_lines():
    errors = parse_go_errors(NAVIDROME_STDOUT)
    # The real `undefined: newClient` errors are captured...
    assert any(e.message == "undefined: newClient" for e in errors)
    assert {e.file for e in errors} == {
        "core/agents/listenbrainz/auth_router_test.go",
        "core/agents/listenbrainz/client_test.go",
        "core/agents/spotify/client_test.go",
    }
    # ...and the `# pkg` headers and `FAIL ... [build failed]` summary lines
    # are NOT mistaken for compile errors.
    assert all(e.file.endswith(".go") for e in errors)
    assert all("build failed" not in e.message for e in errors)


# issue 009 — `go vet` prefixes its diagnostics with a literal `vet: `. The
# original regex anchored the filename to the start of line, so these real
# vet errors parsed to ZERO records and the gate mislabeled a hard compile
# failure as `unverifiable` (then accepted the patch). The parser must strip
# the optional `vet: ` prefix and capture the error identically to a build one.
GO_VET_STDERR = """\
# github.com/gravitational/teleport/lib/kube/proxy
vet: lib/kube/proxy/forwarder_test.go:49:4: unknown field Client in struct literal
"""


def test_parse_go_errors_handles_vet_prefix():
    errors = parse_go_errors(GO_VET_STDERR)
    assert len(errors) == 1
    err = errors[0]
    # The `vet: ` prefix is stripped from the filename, not folded into it.
    assert err.file == "lib/kube/proxy/forwarder_test.go"
    assert err.line == 49
    assert err.message == "unknown field Client in struct literal"
    assert not err.file.startswith("vet")


def test_vet_prefixed_and_plain_errors_parse_identically():
    """A vet-prefixed line and the same line without the prefix are the same
    defect — same file, line, message, and therefore same signature."""
    prefixed = parse_go_errors(
        "vet: a/b_test.go:10:4: unknown field X in struct literal"
    )
    plain = parse_go_errors(
        "a/b_test.go:10:4: unknown field X in struct literal"
    )
    assert len(prefixed) == len(plain) == 1
    assert prefixed[0].signature() == plain[0].signature()


# ── Python parser ─────────────────────────────────────────────────────────

def test_parse_python_errors_extracts_file_and_message():
    errors = parse_python_errors(QUTEBROWSER_STDOUT)
    # Both summary ERROR lines collapse to the single offending module
    # (the `::nodeid` suffix with brackets/spaces is stripped).
    assert {e.file for e in errors} == {"tests/unit/config/test_configfiles.py"}
    # The message is taken from the most recent `E   ...Error:` line.
    assert "ChangelogAfterUpgrade" in errors[0].message


# ── Signatures & baseline diff ──────────────────────────────────────────────

def test_signature_is_line_independent():
    a = BuildError("a/b.go", 10, "undefined: newClient", "raw1")
    b = BuildError("a/b.go", 99, "undefined: newClient", "raw2")
    assert a.signature() == b.signature()


def test_diff_new_errors_subtracts_baseline_by_signature():
    base = BuildCheckResult(
        system="go",
        ok=False,
        errors=[BuildError("a/b.go", 5, "preexisting boom", "r")],
    )
    post = BuildCheckResult(
        system="go",
        ok=False,
        errors=[
            BuildError("a/b.go", 7, "preexisting boom", "r"),       # shifted line, same defect
            BuildError("c/d.go", 1, "undefined: newClient", "r"),   # genuinely new
        ],
    )
    new = diff_new_errors(base, post)
    assert len(new) == 1
    assert new[0].file == "c/d.go"


def test_diff_new_errors_treats_all_as_new_when_baseline_missing():
    post = BuildCheckResult(
        system="go", ok=False,
        errors=[BuildError("c/d.go", 1, "boom", "r")],
    )
    assert diff_new_errors(None, post) == post.errors


# ── Unverifiable (toolchain missing) ────────────────────────────────────────
# These guard the silent-pass bug: a Go repo on a host without `go` produced
# rc=127 + empty errors, which baseline-subtraction cancelled to "no new
# errors", green-lighting every Go patch. The gate must now report
# unverifiable instead of a verified pass.

def test_run_go_without_toolchain_is_unverifiable_not_ok(tmp_path: Path):
    """A go.mod repo built on a host with no `go` → unverifiable, not ok.

    No mock: this relies on the real absence of `go` on the test host. If `go`
    happens to be installed, the premise does not hold and we skip — we never
    fake the toolchain.
    """
    if shutil.which("go") is not None:
        pytest.skip("go toolchain present; cannot exercise the missing-toolchain path")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

    result = run_build_check(tmp_path, system="go")

    assert result.unverifiable is True
    assert result.ok is False
    # The 127 / FileNotFoundError text must NOT have been parsed as a compile
    # error — otherwise baseline-subtraction logic re-enters the buggy path.
    assert result.errors == []
    assert result.skipped is False
    # A genuinely missing toolchain is flagged distinctly so the caller can
    # tell it apart from an un-attributable failure (toolchain ran, failed,
    # produced no parseable error). Only the former earns an unverified pass.
    assert result.toolchain_missing is True


def test_go_build_flags_broken_repo_test_files_via_test_compile(tmp_path: Path):
    if shutil.which("go") is None:
        pytest.skip("go toolchain required for this check")
    (tmp_path / "go.mod").write_text(
        "module example.com/test\n\ngo 1.20\n",
        encoding="utf-8",
    )
    pkg = tmp_path / "events"
    pkg.mkdir()
    (pkg / "message.go").write_text(
        "package events\n\n"
        "type message struct { data string }\n",
        encoding="utf-8",
    )
    (pkg / "message_test.go").write_text(
        "package events\n\n"
        "import \"testing\"\n\n"
        "func TestLegacy(t *testing.T) {\n"
        "\t_ = message{Data: \"x\"}\n"
        "}\n",
        encoding="utf-8",
    )

    result = run_build_check(
        tmp_path,
        system="go",
        go_targets=["./events"],
    )

    assert result.ok is False
    assert any(err.file == "events/message_test.go" for err in result.errors)
    assert "unknown field Data" in " ".join(err.message for err in result.errors)
    assert "go test -c -o /tmp/build-verify-1.test ./events" in result.command


def test_unverifiable_result_is_not_treated_as_ok():
    """An unverifiable result is distinct from every ok shape."""
    unver = BuildCheckResult(system="go", ok=False, unverifiable=True)
    # It is not ok, not skipped, and carries no errors to diff.
    assert not unver.ok
    assert not unver.skipped
    assert unver.errors == []
    # Baseline subtraction over two empty-error results yields nothing — which
    # is precisely why `ok`/`errors` alone could not catch the bug and the
    # explicit `unverifiable` flag is required to refuse the pass.
    assert diff_new_errors(unver, unver) == []


def test_real_go_errors_are_not_flagged_unverifiable():
    """A result with parsed compile errors is a real failure, not unverifiable.

    Bidirectional counterpart: a populated `errors` list (toolchain ran and
    rejected the code) must keep `unverifiable=False`.
    """
    errors = parse_go_errors(TELEPORT_STDERR)
    assert errors  # sanity: the fixture does contain errors
    result = BuildCheckResult(system="go", ok=False, errors=errors, unverifiable=False)
    assert result.unverifiable is False
    assert result.ok is False
    assert len(result.errors) == len(errors)


def test_python_present_is_not_unverifiable(tmp_path: Path):
    """pytest --collect-only on an empty Python repo → not unverifiable.

    The test host has python; an empty collection (pytest rc=5) is a benign
    non-zero exit and must NOT be misclassified as unverifiable.
    """
    if shutil.which("python") is None:
        pytest.skip("python interpreter not on PATH under this name")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")

    result = run_build_check(tmp_path, system="python")

    assert result.unverifiable is False


def test_python_changed_production_import_error_is_build_error(tmp_path: Path):
    if shutil.which("python") is None:
        pytest.skip("python interpreter not on PATH under this name")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("import does_not_exist_for_build_gate\n", encoding="utf-8")

    result = run_build_check(
        tmp_path,
        system="python",
        python_targets=["pkg/mod.py"],
    )

    assert result.ok is False
    assert result.unverifiable is False
    assert len(result.errors) == 1
    assert result.errors[0].file == "pkg/mod.py"
    assert "ModuleNotFoundError" in result.errors[0].message


# ── Detection ───────────────────────────────────────────────────────────────
    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    assert detect_build_system(go) == "go"

    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_build_system(py) == "python"

    js = tmp_path / "js"
    js.mkdir()
    (js / "package.json").write_text("{}\n", encoding="utf-8")
    assert detect_build_system(js) == "node"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_build_system(empty) == "unknown"


def test_detect_build_system_prefers_go_over_python(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_build_system(tmp_path) == "go"


# ── Feedback rendering ──────────────────────────────────────────────────────

def test_render_errors_for_feedback():
    errors = [
        BuildError("lib/service/service.go", 2557, "unknown field 'Auth'", "r"),
        BuildError("tests/x.py", None, "AttributeError: nope", "r"),
    ]
    rendered = render_errors_for_feedback(errors)
    assert "lib/service/service.go:2557: unknown field 'Auth'" in rendered
    assert "tests/x.py: AttributeError: nope" in rendered


def test_python_script_import_uses_script_directory_for_init_path(tmp_path: Path):
    if shutil.which("python") is None:
        pytest.skip("python interpreter not on PATH under this name")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "_init_path.py").write_text("VALUE = 1\n", encoding="utf-8")
    (scripts / "runner.py").write_text(
        "import _init_path\nVALUE = _init_path.VALUE\n",
        encoding="utf-8",
    )

    result = run_build_check(
        tmp_path,
        system="python",
        python_targets=["scripts/runner.py"],
    )

    assert result.ok is True
    assert result.errors == []
