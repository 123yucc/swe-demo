"""Repository command executor.

The repair harness reads and edits the repository on the host, but project
toolchains may only exist inside the SWE-bench Pro instance image.  When
``REPO_EXECUTOR_DOCKER_CONTAINER`` is set, commands are run via ``docker exec``
inside that container after synchronizing the host git diff into ``/app``.
Without that env var this module is a thin local ``subprocess.run`` wrapper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


_CONTAINER_ENV = "REPO_EXECUTOR_DOCKER_CONTAINER"
_WORKDIR_ENV = "REPO_EXECUTOR_CONTAINER_WORKDIR"
_DEFAULT_WORKDIR = "/app"


def docker_executor_enabled() -> bool:
    return bool(os.environ.get(_CONTAINER_ENV))


def executor_container() -> str | None:
    return os.environ.get(_CONTAINER_ENV) or None


def executor_workdir() -> str:
    return os.environ.get(_WORKDIR_ENV) or _DEFAULT_WORKDIR


def _git(
    repo_dir: Path,
    args: list[str],
    timeout: int = 60,
    input_text: str | None = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _docker_exec(
    container: str,
    args: list[str],
    timeout: int,
    input_text: str | None = None,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, bool]:
    cmd = ["docker", "exec"]
    if input_text is not None:
        # docker exec closes container stdin unless -i is explicit. Without
        # this, `git apply -` receives an empty stream even though
        # subprocess.run(input=...) was populated, producing the misleading
        # "No valid patches in input" sync failure.
        cmd.append("-i")
    for key, value in sorted((env or {}).items()):
        if value is not None:
            cmd.extend(["-e", f"{key}={value}"])
    if workdir:
        cmd.extend(["-w", workdir])
    cmd.append(container)
    cmd.extend(args)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out, True
    except (FileNotFoundError, OSError) as exc:
        return 127, f"{type(exc).__name__}: {exc}", False
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False


def _host_patch(repo_dir: Path, timeout: int) -> tuple[int, str]:
    """Return a binary-safe git patch from HEAD to the host working tree."""
    repo_dir = Path(repo_dir)
    rc, untracked_raw = _git(
        repo_dir,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        timeout=timeout,
    )
    if rc != 0:
        return rc, untracked_raw
    untracked = [p for p in untracked_raw.split("\0") if p]

    added_intent: list[str] = []
    if untracked:
        rc_add, out_add = _git(
            repo_dir,
            ["add", "-N", "-f", "--", *untracked],
            timeout=timeout,
        )
        if rc_add == 0:
            added_intent = untracked
        else:
            return rc_add, out_add

    try:
        return _git(repo_dir, ["diff", "--binary", "HEAD"], timeout=timeout)
    finally:
        if added_intent:
            _git(repo_dir, ["reset", "--", *added_intent], timeout=timeout)


def sync_to_executor(repo_dir: Path, timeout: int = 120) -> tuple[int, str]:
    """Synchronize host repo changes into the configured Docker executor."""
    container = executor_container()
    if not container:
        return 0, ""

    repo_dir = Path(repo_dir)
    rc_head, host_head = _git(repo_dir, ["rev-parse", "HEAD"], timeout=timeout)
    if rc_head != 0:
        return rc_head, host_head
    base_ref = host_head.strip()

    workdir = executor_workdir()
    rc_reset, out_reset, timed_out = _docker_exec(
        container,
        ["git", "-C", workdir, "reset", "--hard", base_ref],
        timeout=timeout,
    )
    if timed_out or rc_reset != 0:
        return rc_reset, out_reset

    rc_clean, out_clean, timed_out = _docker_exec(
        container,
        ["git", "-C", workdir, "clean", "-fd"],
        timeout=timeout,
    )
    if timed_out or rc_clean != 0:
        return rc_clean, out_clean

    rc_patch, patch = _host_patch(repo_dir, timeout=timeout)
    if rc_patch != 0:
        return rc_patch, patch
    if not patch.strip():
        return 0, out_reset + out_clean

    rc_apply, out_apply, timed_out = _docker_exec(
        container,
        ["git", "apply", "--whitespace=nowarn", "--binary", "-"],
        timeout=timeout,
        input_text=patch,
        workdir=workdir,
    )
    if timed_out or rc_apply != 0:
        return rc_apply, out_apply
    return 0, out_reset + out_clean + out_apply


def run_repo_command(
    cmd: list[str],
    repo_dir: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str, bool]:
    """Run a command against the repository, locally or in Docker executor."""
    repo_dir = Path(repo_dir)
    container = executor_container()
    if not container:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return 124, out, True
        except (FileNotFoundError, OSError) as exc:
            return 127, f"{type(exc).__name__}: {exc}", False
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False

    rc_sync, out_sync = sync_to_executor(repo_dir, timeout=max(120, min(timeout, 600)))
    if rc_sync != 0:
        # Keep executor plumbing failures distinct from compiler diagnostics.
        # Callers must never feed this text to a model as a code error.
        return rc_sync, "[repo-executor-sync] " + out_sync, False
    return _docker_exec(
        container,
        cmd,
        timeout=timeout,
        workdir=executor_workdir(),
        env=env,
    )


def command_available(cmd: list[str], repo_dir: Path, timeout: int = 30) -> bool:
    rc, _, timed_out = run_repo_command(cmd, repo_dir=repo_dir, timeout=timeout)
    return not timed_out and rc == 0


def copy_from_executor(repo_dir: Path, rel_paths: list[str], timeout: int = 60) -> None:
    """Best-effort copy of generated artifacts from Docker executor to host."""
    container = executor_container()
    if not container:
        return
    workdir = executor_workdir().rstrip("/")
    repo_dir = Path(repo_dir)
    for rel in rel_paths:
        rel_norm = rel.replace("\\", "/").lstrip("/")
        dest = repo_dir / rel_norm
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["docker", "cp", f"{container}:{workdir}/{rel_norm}", str(dest)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
