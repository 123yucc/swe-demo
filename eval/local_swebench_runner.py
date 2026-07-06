#!/usr/bin/env python3
"""Local single-host SWE-bench Pro patch-generation and evaluation runner.

The runner reads a local manifest, expands it into (model, issue) tasks, runs
the repair harness inside each issue Docker image, evaluates the generated
patch locally, and aggressively removes containers/images after every task.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKDIR = REPO_ROOT / "workdir"
EVAL_SCRIPT = REPO_ROOT / "eval" / "SWE-bench_Pro-os" / "swe_bench_pro_eval.py"
RUN_SCRIPTS_DIR = REPO_ROOT / "eval" / "SWE-bench_Pro-os" / "run_scripts"

TERMINAL_OUTPUTS = {
    "prediction.json",
    "run_metrics.json",
    "patch.diff",
    "patch_outcome.json",
}

DOCKER_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
SECRET_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_PRO_API_KEY",
}
DOCKER_PULL_TIMEOUT_SECONDS = 20 * 60


@dataclass(frozen=True)
class ModelSpec:
    name: str
    env: dict[str, str]
    output_subdir: str


@dataclass(frozen=True)
class IssueSpec:
    issue_name: str
    issue_dir: Path
    metadata_path: Path


@dataclass(frozen=True)
class TaskSpec:
    model: ModelSpec
    issue: IssueSpec

    @property
    def key(self) -> str:
        return f"{self.model.output_subdir}:{self.issue.issue_name}"

    @property
    def output_dir(self) -> Path:
        return self.issue.issue_dir / self.model.output_subdir


@dataclass(frozen=True)
class RunContext:
    run_id: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_slug(value: str, limit: int = 64) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip(".-_").lower()
    slug = re.sub(r"-{2,}", "-", slug) or "unknown"
    if len(slug) <= limit:
        return slug
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]
    return f"{slug[: limit - 11]}-{digest}"


def model_output_dir_name(model_name: str) -> str:
    return f"outputs_{safe_slug(model_name)}"


def build_run_id() -> str:
    seed = f"{datetime.now(timezone.utc).isoformat()}-{os.getpid()}-{random.getrandbits(32):08x}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def container_name(kind: str, task: TaskSpec, run_id: str) -> str:
    raw = f"swe_{kind}_{task.model.output_subdir}_{task.issue.issue_name}_{run_id}"
    slug = safe_slug(raw, limit=48)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug}_{digest}"


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def load_eval_module():
    if not EVAL_SCRIPT.exists():
        raise FileNotFoundError(f"SWE-bench Pro evaluator not found: {EVAL_SCRIPT}")
    spec = importlib.util.spec_from_file_location("swe_bench_pro_eval_local", EVAL_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import evaluator from {EVAL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def docker_cmd(
    args: list[str],
    log_path: Path | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *args],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if log_path is not None:
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"$ docker {' '.join(redact_docker_args(args))}\n")
                if stdout:
                    f.write(stdout)
                    if not stdout.endswith("\n"):
                        f.write("\n")
                f.write(f"[timeout] docker command exceeded {timeout} seconds\n")
        raise RuntimeError(f"docker {' '.join(args)} timed out after {timeout} seconds") from exc
    if log_path is not None and result.stdout:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"$ docker {' '.join(redact_docker_args(args))}\n")
            f.write(result.stdout)
            if not result.stdout.endswith("\n"):
                f.write("\n")
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed with exit {result.returncode}")
    return result


def redact_docker_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    for arg in args:
        hidden = arg
        for key in SECRET_ENV_KEYS:
            prefix = f"{key}="
            if arg.startswith(prefix):
                hidden = f"{prefix}<redacted>"
                break
        redacted.append(hidden)
    return redacted


def docker_available() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker executable not found on PATH")
    result = docker_cmd(["info"])
    if result.returncode != 0:
        raise RuntimeError("docker daemon is not available:\n" + result.stdout[-2000:])


def read_total_memory_gb() -> float | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) / 1024 / 1024
    return None


def auto_workers(per_task_gb: float, reserve_gb: float, hard_cap: int | None) -> int:
    total = read_total_memory_gb()
    cpu = os.cpu_count() or 1
    if total is None:
        workers = min(cpu, 2)
    else:
        usable = max(total - reserve_gb, per_task_gb)
        workers = max(1, int(usable // per_task_gb))
        workers = min(workers, cpu)
    if hard_cap is not None:
        workers = min(workers, hard_cap)
    return max(1, workers)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def normalize_issue_name(value: str) -> str:
    value = str(value).strip()
    if value.startswith("swe_issue_"):
        return value
    if re.fullmatch(r"\d+", value):
        return f"swe_issue_{int(value):03d}"
    return value


def issue_from_entry(entry: Any, workdir: Path) -> IssueSpec:
    if isinstance(entry, dict):
        issue_name = normalize_issue_name(entry.get("issue") or entry.get("name") or entry.get("issue_name"))
        issue_dir = Path(entry.get("issue_dir") or workdir / issue_name)
    else:
        issue_name = normalize_issue_name(str(entry))
        issue_dir = workdir / issue_name
    if not issue_dir.is_absolute():
        issue_dir = (REPO_ROOT / issue_dir).resolve()
    metadata_path = issue_dir / "artifacts" / "instance_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing instance metadata: {metadata_path}")
    return IssueSpec(issue_name=issue_dir.name, issue_dir=issue_dir, metadata_path=metadata_path)


def normalize_model_entry(entry: Any) -> ModelSpec:
    if isinstance(entry, str):
        name = entry
        env = {"ANTHROPIC_MODEL": entry}
        return ModelSpec(name=name, env=env, output_subdir=model_output_dir_name(name))

    if not isinstance(entry, dict):
        raise TypeError(f"Invalid model entry: {entry!r}")

    name = str(entry.get("name") or entry.get("model") or entry.get("ANTHROPIC_MODEL") or entry.get("OPENAI_MODEL") or "unknown")
    env = {str(k): str(v) for k, v in dict(entry.get("env") or {}).items() if v is not None}
    backend = entry.get("backend") or entry.get("MODEL_BACKEND")
    if backend:
        env["MODEL_BACKEND"] = str(backend)
    if "model" in entry and "ANTHROPIC_MODEL" not in env and "OPENAI_MODEL" not in env and "CODEX_PRO_MODEL" not in env:
        if str(env.get("MODEL_BACKEND", "anthropic")).lower() in {"openai", "codex", "codex-pro"}:
            env["OPENAI_MODEL"] = str(entry["model"])
        else:
            env["ANTHROPIC_MODEL"] = str(entry["model"])
    output_subdir = str(entry.get("output_subdir") or model_output_dir_name(name))
    return ModelSpec(name=name, env=env, output_subdir=output_subdir)


def expand_manifest(manifest_path: Path, workdir: Path) -> tuple[list[TaskSpec], dict[str, Any]]:
    manifest = load_json(manifest_path)
    if isinstance(manifest, list):
        manifest = {"tasks": manifest}
    if not isinstance(manifest, dict):
        raise TypeError("Manifest must be a JSON object or a task list")

    models = [normalize_model_entry(m) for m in manifest.get("models", [])]
    model_by_name = {m.name: m for m in models}
    model_by_output = {m.output_subdir: m for m in models}

    tasks: list[TaskSpec] = []
    if manifest.get("tasks"):
        for raw_task in manifest["tasks"]:
            if not isinstance(raw_task, dict):
                raise TypeError(f"Invalid task entry: {raw_task!r}")
            raw_model = raw_task.get("model") or raw_task.get("model_name") or raw_task.get("output_subdir")
            if isinstance(raw_model, dict):
                model = normalize_model_entry(raw_model)
            elif raw_model in model_by_name:
                model = model_by_name[str(raw_model)]
            elif raw_model in model_by_output:
                model = model_by_output[str(raw_model)]
            else:
                merged = dict(raw_task)
                if raw_model:
                    merged["model"] = raw_model
                model = normalize_model_entry(merged)
            issue = issue_from_entry(raw_task.get("issue") or raw_task.get("issue_name") or raw_task, workdir)
            tasks.append(TaskSpec(model=model, issue=issue))
    else:
        if not models:
            raise ValueError("Manifest needs either tasks[] or models[]")
        issues = [issue_from_entry(i, workdir) for i in manifest.get("issues", [])]
        if not issues:
            raise ValueError("Manifest needs issues[] when tasks[] is omitted")
        for model in models:
            for issue in issues:
                tasks.append(TaskSpec(model=model, issue=issue))

    seen: set[str] = set()
    unique_tasks: list[TaskSpec] = []
    for task in tasks:
        if task.key in seen:
            continue
        seen.add(task.key)
        unique_tasks.append(task)
    return unique_tasks, manifest


def dockerhub_tag_from_image_name(image_name: str) -> str | None:
    if not image_name or ":" not in image_name:
        return None
    tag = image_name.rsplit("/", 1)[-1].replace(":", "-")
    return tag[:128]


def image_candidates(metadata: dict[str, Any], dockerhub_users: list[str]) -> list[str]:
    candidates: list[str] = []
    tag = str(metadata.get("dockerhub_tag") or "").strip()
    if not tag:
        tag = dockerhub_tag_from_image_name(str(metadata.get("image_name") or "")) or ""
    if tag:
        candidates.extend(f"{user}/sweap-images:{tag}" for user in dockerhub_users)
    raw = str(metadata.get("image_name") or "").strip()
    if raw:
        candidates.append(raw)
    return list(dict.fromkeys(candidates))


def pull_first_image(candidates: list[str], platform: str | None, log_path: Path) -> str:
    last = ""
    for image in candidates:
        inspect = docker_cmd(["image", "inspect", image], log_path=log_path)
        if inspect.returncode == 0:
            return image
        args = ["pull"]
        if platform:
            args.extend(["--platform", platform])
        args.append(image)
        result = docker_cmd(args, log_path=log_path, timeout=DOCKER_PULL_TIMEOUT_SECONDS)
        if result.returncode == 0:
            return image
        last = result.stdout
        inspect = docker_cmd(["image", "inspect", image], log_path=log_path)
        if inspect.returncode == 0:
            return image
    raise RuntimeError("No Docker image could be pulled or found locally:\n" + last[-2000:])


def memory_to_docker_arg(memory_gb: float | None) -> str | None:
    if memory_gb is None or memory_gb <= 0:
        return None
    if float(memory_gb).is_integer():
        return f"{int(memory_gb)}g"
    return f"{memory_gb:g}g"


def build_env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sorted(env.items()):
        if value is None or value == "":
            continue
        args.extend(["-e", f"{key}={value}"])
    return args


def create_container(
    *,
    name: str,
    image: str,
    command: list[str],
    log_path: Path,
    env: dict[str, str] | None = None,
    volumes: list[tuple[Path, str]] | None = None,
    memory_gb: float | None = None,
    platform: str | None = None,
    entrypoint: str | None = None,
) -> None:
    args = ["create", "--name", name]
    if platform:
        args.extend(["--platform", platform])
    memory = memory_to_docker_arg(memory_gb)
    if memory:
        args.extend([f"--memory={memory}", f"--memory-swap={memory}"])
    if entrypoint:
        args.extend(["--entrypoint", entrypoint])
    for host_path, container_path in volumes or []:
        args.extend(["-v", f"{host_path}:{container_path}"])
    args.extend(build_env_args(env or {}))
    args.append(image)
    args.extend(command)
    docker_cmd(["rm", "-f", name], log_path=log_path)
    docker_cmd(args, log_path=log_path, check=True)


def start_container_and_wait(name: str, log_path: Path) -> int:
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"$ docker start -a {name}\n")
        log.flush()
        proc = subprocess.Popen(
            ["docker", "start", "-a", name],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.wait()
    inspect = docker_cmd(["inspect", "-f", "{{.State.ExitCode}}", name], log_path=log_path)
    try:
        return int((inspect.stdout or "").strip().splitlines()[-1])
    except Exception:
        return proc.returncode or 1


def start_container_detached(name: str, log_path: Path) -> None:
    result = docker_cmd(["start", name], log_path=log_path)
    if result.returncode != 0:
        raise RuntimeError(f"docker start {name} failed with exit {result.returncode}")


def cleanup_docker(names: list[str], images: list[str], log_path: Path, prune: bool) -> None:
    with DOCKER_LOCK:
        for name in names:
            docker_cmd(["rm", "-f", name], log_path=log_path)
        if prune:
            for image in dict.fromkeys(images):
                docker_cmd(["rmi", "-f", image], log_path=log_path)
            docker_cmd(["image", "prune", "-a", "-f"], log_path=log_path)
            docker_cmd(["container", "prune", "-f"], log_path=log_path)


def write_generation_script(task: TaskSpec, force_restart: bool) -> Path:
    raise RuntimeError(
        "in-container harness bootstrap is disabled; generation must run the "
        "host harness with the SWE-bench image as a repo executor sandbox"
    )


def _legacy_in_container_generation_script(task: TaskSpec, force_restart: bool) -> Path:
    script_path = task.issue.issue_dir / f"_{task.model.output_subdir}_run_harness.sh"
    force_arg = " --force-restart" if force_restart else ""
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NO_PROXY="127.0.0.1,localhost,${{NO_PROXY:-}}"
export no_proxy="127.0.0.1,localhost,${{no_proxy:-}}"
for d in /usr/local/go/bin /usr/lib/go/bin /usr/lib/go-*/bin /opt/go/bin; do
  [ -d "$d" ] && export PATH="$d:$PATH"
done
WHEELS=/demo/eval/docker/wheels
HARNESS_ROOT=/tmp/demo-harness
HARNESS_PY=$HARNESS_ROOT/python311/bin/python3.11
HARNESS_LIB=$HARNESS_ROOT/python311/lib/libpython3.11.so.1.0
HARNESS_VENV=$HARNESS_ROOT/venv
HARNESS_REQUIREMENTS=/demo/requirements.lock
ALPINE_COMPAT_WHEELS=
INSTANCE_JSON=/demo/{task.issue.metadata_path.relative_to(REPO_ROOT).as_posix()}

mkdir -p "$HARNESS_ROOT"

repair_harness_python_for_alpine() {{
  [ -x "$HARNESS_PY" ] || return 0
  [ -f "$HARNESS_LIB" ] || return 0
  command -v patchelf >/dev/null 2>&1 || return 0
  if patchelf --print-needed "$HARNESS_PY" 2>/dev/null | grep -Fxq '$ORIGIN/../lib/libpython3.11.so.1.0'; then
    patchelf --replace-needed '$ORIGIN/../lib/libpython3.11.so.1.0' libpython3.11.so.1.0 "$HARNESS_PY" >/dev/null 2>&1 || true
  fi
  patchelf --set-rpath "$HARNESS_ROOT/python311/lib:/usr/lib" "$HARNESS_PY" >/dev/null 2>&1 || true
  patchelf --set-rpath "$HARNESS_ROOT/python311/lib:/usr/lib" "$HARNESS_LIB" >/dev/null 2>&1 || true
  if ! patchelf --print-needed "$HARNESS_LIB" 2>/dev/null | grep -Fxq 'libfts.so.0'; then
    patchelf --add-needed libfts.so.0 "$HARNESS_LIB" >/dev/null 2>&1 || true
  fi
}}

prepare_harness_requirements() {{
  # local_swebench_runner always provides a local instance metadata JSON, so
  # the HuggingFace dataset path is unused and its heavy deps are unnecessary.
  if [ -f "$INSTANCE_JSON" ]; then
    HARNESS_REQUIREMENTS=$HARNESS_ROOT/requirements.instance-json.lock
    grep -vE '^(datasets|pyarrow)==' /demo/requirements.lock > "$HARNESS_REQUIREMENTS"
  fi
}}

prepare_alpine_compat_wheels() {{
  [ -f /etc/alpine-release ] || return 0
  ALPINE_COMPAT_WHEELS=$HARNESS_ROOT/wheels-compat
  mkdir -p "$ALPINE_COMPAT_WHEELS"
  "$PYBIN" - "$WHEELS" "$ALPINE_COMPAT_WHEELS" <<'PY'
import base64
import csv
import hashlib
import pathlib
import sys
import tempfile
import zipfile

src = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)

for wheel_in in src.glob("*.whl"):
    name = wheel_in.name
    if "manylinux" not in name:
        continue
    stem = name[:-4]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4:
        continue
    distver, py_tag, abi_tag, plat_tag = parts
    if "linux_x86_64" in plat_tag.split("."):
        continue
    new_plat_tag = plat_tag + ".linux_x86_64"
    new_name = "-".join((distver, py_tag, abi_tag, new_plat_tag)) + ".whl"

    work = pathlib.Path(tempfile.mkdtemp(prefix="wheel-edit-"))
    with zipfile.ZipFile(wheel_in) as zf:
        zf.extractall(work)

    wheel_files = list(work.glob("*.dist-info/WHEEL"))
    if not wheel_files:
        continue
    wheel_file = wheel_files[0]
    out_lines = []
    for line in wheel_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("Tag: "):
            tag = line[5:]
            tag_parts = tag.split("-", 2)
            if len(tag_parts) == 3:
                plat = tag_parts[2]
                if "linux_x86_64" not in plat.split("."):
                    plat = plat + ".linux_x86_64"
                line = "Tag: " + tag_parts[0] + "-" + tag_parts[1] + "-" + plat
        out_lines.append(line)
    wheel_file.write_text("\\n".join(out_lines) + "\\n", encoding="utf-8")

    record_files = list(work.glob("*.dist-info/RECORD"))
    if not record_files:
        continue
    record_file = record_files[0]
    record_rel = record_file.relative_to(work).as_posix()
    rows = []
    for path in sorted(p for p in work.rglob("*") if p.is_file()):
        rel = path.relative_to(work).as_posix()
        if rel == record_rel:
            rows.append((rel, "", ""))
            continue
        data = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append((rel, "sha256=" + digest, str(len(data))))
    with record_file.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    out_whl = out / new_name
    with zipfile.ZipFile(out_whl, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in work.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(work).as_posix())
PY
}}

if [ -f "$WHEELS/python311-linux.tar.gz" ]; then
  if ! "$HARNESS_PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    rm -rf "$HARNESS_ROOT/python311"
    mkdir -p "$HARNESS_ROOT/python311"
    tar -xzf "$WHEELS/python311-linux.tar.gz" -C "$HARNESS_ROOT/python311" --strip-components=1
  fi
fi

if [ -f /etc/alpine-release ] && command -v apk >/dev/null 2>&1; then
  if ! "$HARNESS_PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    apk add --no-cache gcompat libstdc++ musl-fts patchelf >/dev/null 2>&1 || true
    if [ -e /lib/ld-linux-x86-64.so.2 ] && [ ! -e /lib64/ld-linux-x86-64.so.2 ]; then
      mkdir -p /lib64
      ln -sf /lib/ld-linux-x86-64.so.2 /lib64/ld-linux-x86-64.so.2
    fi
    repair_harness_python_for_alpine
  fi
fi

PYBIN=""
for candidate in "$HARNESS_PY" /usr/bin/python3.11 /opt/python311/bin/python3.11 "$(command -v python3 2>/dev/null || true)"; do
  [ -n "$candidate" ] || continue
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    PYBIN="$candidate"
    break
  fi
done

if [ -z "$PYBIN" ]; then
  echo "No usable Python 3.11 runtime found for harness bootstrap" >&2
  echo "[harness-diagnostics] os-release:" >&2
  cat /etc/os-release >&2 2>/dev/null || true
  echo "[harness-diagnostics] uname=$(uname -a 2>/dev/null || true)" >&2
  echo "[harness-diagnostics] loader candidates:" >&2
  ls -l /lib/ld-linux* /lib64/ld-linux* /usr/glibc-compat/lib/ld-linux* >&2 2>/dev/null || true
  if [ -e "$HARNESS_PY" ]; then
    echo "[harness-diagnostics] HARNESS_PY=$HARNESS_PY" >&2
    file "$HARNESS_PY" >&2 2>&1 || true
    ldd "$HARNESS_PY" >&2 2>&1 || true
    if command -v patchelf >/dev/null 2>&1; then
      echo "[harness-diagnostics] HARNESS_PY needed:" >&2
      patchelf --print-needed "$HARNESS_PY" >&2 2>&1 || true
      echo "[harness-diagnostics] HARNESS_PY rpath:" >&2
      patchelf --print-rpath "$HARNESS_PY" >&2 2>&1 || true
    fi
    "$HARNESS_PY" -V >&2 2>&1 || true
  fi
  if [ -e "$HARNESS_LIB" ]; then
    echo "[harness-diagnostics] HARNESS_LIB=$HARNESS_LIB" >&2
    file "$HARNESS_LIB" >&2 2>&1 || true
    ldd "$HARNESS_LIB" >&2 2>&1 || true
    if command -v patchelf >/dev/null 2>&1; then
      echo "[harness-diagnostics] HARNESS_LIB needed:" >&2
      patchelf --print-needed "$HARNESS_LIB" >&2 2>&1 || true
      echo "[harness-diagnostics] HARNESS_LIB rpath:" >&2
      patchelf --print-rpath "$HARNESS_LIB" >&2 2>&1 || true
    fi
  else
    echo "[harness-diagnostics] HARNESS_PY missing: $HARNESS_PY" >&2
  fi
  for candidate in /usr/bin/python3.11 /opt/python311/bin/python3.11 "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    echo "[harness-diagnostics] candidate=$candidate" >&2
    "$candidate" -V >&2 2>&1 || true
  done
  exit 127
fi

if [ ! -x "$HARNESS_VENV/bin/python" ] || ! "$HARNESS_VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
  rm -rf "$HARNESS_VENV"
  "$PYBIN" -m venv "$HARNESS_VENV"
fi

PYBIN="$HARNESS_VENV/bin/python"

if ! "$PYBIN" -m pip --version >/dev/null 2>&1; then
  "$PYBIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "$PYBIN" -m pip --version >/dev/null 2>&1; then
  if [ -f "$WHEELS/get-pip.py" ]; then
    "$PYBIN" "$WHEELS/get-pip.py" --no-index --find-links "$WHEELS" -q || "$PYBIN" "$WHEELS/get-pip.py" -q
  else
    echo "pip bootstrap failed and $WHEELS/get-pip.py does not exist" >&2
    exit 127
  fi
fi

echo "[harness-preflight] python=$("$PYBIN" -V 2>&1)"
echo "[harness-preflight] pip=$("$PYBIN" -m pip --version 2>&1)"
echo "[harness-preflight] PATH=$PATH"
for tool in git go node npm python3; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "[harness-preflight] $tool=$(command -v "$tool")"
  else
    echo "[harness-preflight] $tool=MISSING"
  fi
done
if [ -d /app ]; then
  git -C /app rev-parse --show-toplevel >/dev/null 2>&1 && echo "[harness-preflight] repo=/app git-ok" || echo "[harness-preflight] repo=/app git-unavailable"
fi

if [ ! -d "$WHEELS" ] || ! compgen -G "$WHEELS/*.whl" >/dev/null; then
  echo "wheelhouse is missing; expected predownloaded wheels in $WHEELS" >&2
  exit 127
fi

prepare_harness_requirements
prepare_alpine_compat_wheels
echo "[harness-preflight] requirements=$HARNESS_REQUIREMENTS"
if [ -n "$ALPINE_COMPAT_WHEELS" ]; then
  echo "[harness-preflight] compat_wheels=$ALPINE_COMPAT_WHEELS"
fi

PIP_FIND_LINKS=(--find-links "$WHEELS")
if [ -n "$ALPINE_COMPAT_WHEELS" ]; then
  PIP_FIND_LINKS+=(--find-links "$ALPINE_COMPAT_WHEELS")
fi

"$PYBIN" -m pip install --no-index "${{PIP_FIND_LINKS[@]}}" -q --upgrade pip setuptools wheel
"$PYBIN" -m pip install --no-index "${{PIP_FIND_LINKS[@]}}" -q -r "$HARNESS_REQUIREMENTS"
REPO_LANG="$("$PYBIN" - "$INSTANCE_JSON" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
print(str(data.get("repo_language") or data.get("language") or "").lower())
PY
)"
if [ "$REPO_LANG" = "go" ] && ! command -v go >/dev/null 2>&1; then
  echo "Go repository but go toolchain is not visible in the generation container PATH" >&2
  exit 127
fi

"$PYBIN" - <<'PY' &
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{{"status":"ok","results":[]}}')
    def do_POST(self):
        self.do_GET()
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", 9030), H).serve_forever()
PY
sleep 1

cd /demo
"$PYBIN" -m src.main \\
  --instance-json "$INSTANCE_JSON" \\
  --repo-dir /app \\
  --output-dir /demo/{task.output_dir.relative_to(REPO_ROOT).as_posix()}{force_arg}
"""
    script_path.write_text(content, encoding="utf-8", newline="\n")
    return script_path


def should_skip_task(task: TaskSpec, force_restart: bool, redo_eval: bool, eval_only: bool) -> tuple[bool, str]:
    if force_restart or eval_only:
        return False, "force restart requested"
    output_dir = task.output_dir
    if not output_dir.exists():
        return False, "output_subdir missing"
    if (output_dir / "checkpoint.json").exists():
        return False, "checkpoint found; resume harness"
    if redo_eval and not (output_dir / "eval_result" / "eval_summary.json").exists():
        return False, "redo eval requested"
    if (output_dir / "runner_task.json").exists():
        try:
            status = load_json(output_dir / "runner_task.json").get("status")
            if status == "success":
                return True, "runner_task.json already success"
        except Exception:
            pass
    if (output_dir / "eval_result" / "eval_summary.json").exists():
        return True, "eval_result/eval_summary.json exists"
    if TERMINAL_OUTPUTS <= {p.name for p in output_dir.iterdir() if p.is_file()}:
        return False, "terminal harness outputs exist; eval missing"
    return False, "output_subdir exists but is incomplete"


def can_eval_existing_patch(task: TaskSpec, redo_eval: bool, eval_only: bool = False) -> bool:
    output_dir = task.output_dir
    if (output_dir / "eval_result" / "eval_summary.json").exists() and not eval_only:
        return False
    patch_path = output_dir / "patch.diff"
    if not patch_path.exists():
        return False
    if not patch_has_effective_diff(patch_path):
        return False
    terminal_outputs_exist = TERMINAL_OUTPUTS <= {
        p.name for p in output_dir.iterdir() if p.is_file()
    }
    return eval_only or redo_eval or terminal_outputs_exist


def patch_has_effective_diff(patch_path: Path) -> bool:
    """Return True only when patch.diff contains at least one file diff."""
    try:
        text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.startswith("diff --git ") for line in text.splitlines())


def run_generation(
    task: TaskSpec,
    candidates: list[str],
    env: dict[str, str],
    memory_gb: float | None,
    platform: str | None,
    force_restart: bool,
    log_path: Path,
    run: RunContext,
) -> tuple[str, int]:
    cname = container_name("gen", task, run.run_id)
    with DOCKER_LOCK:
        image = pull_first_image(candidates, platform=platform, log_path=log_path)
        create_container(
            name=cname,
            image=image,
            command=["-c", "trap 'exit 0' TERM INT; while true; do sleep 3600; done"],
            log_path=log_path,
            env={
                "NO_PROXY": env.get("NO_PROXY", ""),
                "no_proxy": env.get("no_proxy", ""),
            },
            volumes=None,
            memory_gb=memory_gb,
            platform=platform,
            entrypoint="/bin/bash",
        )
        start_container_detached(cname, log_path)

    host_env = dict(env)
    host_env["REPO_EXECUTOR_DOCKER_CONTAINER"] = cname
    host_env["REPO_EXECUTOR_CONTAINER_WORKDIR"] = "/app"
    host_env["NO_PROXY"] = "127.0.0.1,localhost," + host_env.get("NO_PROXY", "")
    host_env["no_proxy"] = "127.0.0.1,localhost," + host_env.get("no_proxy", "")

    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--instance-json",
        str(task.issue.metadata_path),
        "--repo-dir",
        str(task.issue.issue_dir / "repo"),
        "--output-dir",
        str(task.output_dir),
    ]
    if force_restart:
        cmd.append("--force-restart")

    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(
            "[harness-preflight] mode=host-harness docker-sandbox="
            f"{cname} workdir=/app\n"
        )
        log.write(f"[harness-preflight] run_id={run.run_id}\n")
        log.write(
            "$ "
            + " ".join(str(part) for part in cmd)
            + "\n"
        )
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=host_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.wait()
    return image, proc.returncode or 0


def run_eval(
    task: TaskSpec,
    metadata: dict[str, Any],
    image: str,
    memory_gb: float | None,
    platform: str | None,
    log_path: Path,
    eval_module: Any,
    run: RunContext,
) -> dict[str, Any]:
    patch_path = task.output_dir / "patch.diff"
    if not patch_path.exists():
        raise FileNotFoundError(f"Generated patch not found: {patch_path}")

    uid = str(metadata["instance_id"])
    eval_dir = task.output_dir / "eval_result"
    workspace_dir = eval_dir / uid / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    patch = patch_path.read_text(encoding="utf-8", errors="replace")
    files, entryscript = eval_module.assemble_workspace_files(uid, str(RUN_SCRIPTS_DIR), patch, metadata)
    eval_module.write_files_local(str(workspace_dir), files)
    eval_module.write_patch_snapshot(str(eval_dir), uid, "", patch)

    cname = container_name("eval", task, run.run_id)
    with DOCKER_LOCK:
        create_container(
            name=cname,
            image=image,
            command=["-c", "bash /workspace/entryscript.sh"],
            log_path=log_path,
            volumes=[(workspace_dir.resolve(), "/workspace")],
            memory_gb=memory_gb,
            platform=platform,
            entrypoint="/bin/bash",
        )
    exit_code = start_container_and_wait(cname, log_path)

    output = eval_module.collect_outputs_local(str(workspace_dir), str(eval_dir), uid, "")
    if output is None:
        output = eval_module.synthesize_output_from_logs(str(workspace_dir), uid)
        if output is None:
            raise RuntimeError(f"Evaluation produced no output for {uid}; inspect {log_path}")
        output.update(eval_module.build_debug_payload(output.get("stdout", ""), output.get("stderr", ""), metadata))
        write_json_atomic(eval_dir / uid / "_output.json", output)
    eval_module.save_entryscript_copy(str(eval_dir), uid, "", entryscript)

    verdict = eval_module.evaluate_expected_tests(output, metadata)
    expected = set(verdict["expected_tests"])
    passed_tests = set(verdict["passed_expected_tests"])
    resolved = verdict["resolved"]
    summary = {
        "instance_id": uid,
        "resolved": resolved,
        "container_exit_code": exit_code,
        "expected_tests": sorted(expected),
        "passed_tests": sorted(passed_tests),
        "expected_test_statuses": verdict["expected_test_statuses"],
        "actual_passed_tests": verdict["actual_passed_tests"],
        "passed_count": len(passed_tests),
        "total_tests": len(expected),
        "parsed_status_counts": verdict["status_counts"],
        "eval_output": str(eval_dir / uid / "_output.json"),
        "finished_at": utc_now(),
    }
    write_json_atomic(eval_dir / "eval_results.json", {uid: resolved})
    write_json_atomic(eval_dir / "eval_summary.json", summary)
    return summary


def load_runner_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"started_at": utc_now(), "tasks": {}}
    try:
        data = load_json(path)
        if isinstance(data, dict):
            data.setdefault("tasks", {})
            return data
    except Exception:
        pass
    return {"started_at": utc_now(), "tasks": {}}


def update_state(path: Path, task: TaskSpec, payload: dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_runner_state(path)
        state["updated_at"] = utc_now()
        state["tasks"][task.key] = payload
        write_json_atomic(path, state)


def run_task(
    task: TaskSpec,
    *,
    base_env: dict[str, str],
    dockerhub_users: list[str],
    memory_gb: float | None,
    platform: str | None,
    force_restart: bool,
    redo_eval: bool,
    prune: bool,
    state_file: Path,
    eval_module: Any,
    run: RunContext,
    eval_only: bool,
) -> dict[str, Any]:
    output_dir = task.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    gen_log = logs_dir / "generate.log"
    eval_log = logs_dir / "eval.log"
    cleanup_log = logs_dir / "docker_cleanup.log"
    task_status_path = output_dir / "runner_task.json"

    skip, reason = should_skip_task(task, force_restart=force_restart, redo_eval=redo_eval, eval_only=eval_only)
    if skip:
        payload = {
            "status": "skipped",
            "reason": reason,
            "issue": task.issue.issue_name,
            "model": task.model.name,
            "output_subdir": task.model.output_subdir,
            "updated_at": utc_now(),
        }
        write_json_atomic(task_status_path, payload)
        update_state(state_file, task, payload)
        return payload

    started_at = utc_now()
    update_state(state_file, task, {
        "status": "running",
        "issue": task.issue.issue_name,
        "model": task.model.name,
        "output_subdir": task.model.output_subdir,
        "run_id": run.run_id,
        "started_at": started_at,
    })

    metadata = load_json(task.issue.metadata_path)
    images_seen: list[str] = []
    names = [
        container_name("gen", task, run.run_id),
        container_name("eval", task, run.run_id),
    ]
    env = dict(base_env)
    env.update(task.model.env)
    env["NO_PROXY"] = "127.0.0.1,localhost," + env.get("NO_PROXY", "")
    env["no_proxy"] = "127.0.0.1,localhost," + env.get("no_proxy", "")

    try:
        candidates = image_candidates(metadata, dockerhub_users)
        if not candidates:
            raise RuntimeError(f"No Docker image candidates for {task.issue.issue_name}")

        if can_eval_existing_patch(task, redo_eval=redo_eval, eval_only=eval_only):
            with DOCKER_LOCK:
                image = pull_first_image(candidates, platform=platform, log_path=eval_log)
            images_seen.append(image)
        else:
            if eval_only:
                raise RuntimeError(f"eval-only requested but patch.diff is unavailable for {task.issue.issue_name}")
            image, gen_exit = run_generation(
                task,
                candidates=candidates,
                env=env,
                memory_gb=memory_gb,
                platform=platform,
                force_restart=force_restart,
                log_path=gen_log,
                run=run,
            )
            images_seen.append(image)
            if gen_exit != 0:
                raise RuntimeError(f"patch generation container exited {gen_exit}; see {gen_log}")
            if not patch_has_effective_diff(output_dir / "patch.diff"):
                raise RuntimeError(
                    f"patch generation produced no effective diff for "
                    f"{task.issue.issue_name}; skipping eval"
                )

        eval_summary = run_eval(
            task,
            metadata=metadata,
            image=image,
            memory_gb=memory_gb,
            platform=platform,
            log_path=eval_log,
            eval_module=eval_module,
            run=run,
        )
        payload = {
            "status": "success",
            "issue": task.issue.issue_name,
            "instance_id": metadata.get("instance_id"),
            "model": task.model.name,
            "output_subdir": task.model.output_subdir,
            "run_id": run.run_id,
            "resolved": eval_summary["resolved"],
            "started_at": started_at,
            "finished_at": utc_now(),
            "patch_path": str(output_dir / "patch.diff"),
            "eval_summary": str(output_dir / "eval_result" / "eval_summary.json"),
            "logs_dir": str(logs_dir),
        }
        return payload
    except Exception as exc:
        payload = {
            "status": "failed",
            "issue": task.issue.issue_name,
            "model": task.model.name,
            "output_subdir": task.model.output_subdir,
            "run_id": run.run_id,
            "error": repr(exc),
            "started_at": started_at,
            "finished_at": utc_now(),
            "logs_dir": str(logs_dir),
        }
        return payload
    finally:
        cleanup_docker(names=names, images=images_seen, log_path=cleanup_log, prune=prune)
        final_payload = locals().get("payload")
        if isinstance(final_payload, dict):
            write_json_atomic(task_status_path, final_payload)
            update_state(state_file, task, final_payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SWE-bench Pro locally from a manifest")
    parser.add_argument("--manifest", required=True, type=Path, help="Local JSON manifest path")
    parser.add_argument("--workdir", default=WORKDIR, type=Path, help="Directory containing swe_issue_* folders")
    parser.add_argument("--state-file", default=WORKDIR / "local_runner_state.json", type=Path)
    parser.add_argument("--run-id", default=None, help="Stable identifier for this runner invocation")
    parser.add_argument("--dockerhub-user", default="jefzda", help="Primary DockerHub user for sweap-images")
    parser.add_argument("--fallback-dockerhub-user", action="append", default=["123yucc"])
    parser.add_argument("--max-workers", type=int, default=None, help="Override automatic concurrency")
    parser.add_argument("--per-task-gb", type=float, default=6.0, help="Memory budget per task for auto concurrency and docker --memory")
    parser.add_argument("--reserve-gb", type=float, default=6.0, help="Host memory reserve for auto concurrency")
    parser.add_argument("--hard-cap-workers", type=int, default=8, help="Upper bound for auto concurrency")
    parser.add_argument("--no-memory-limit", action="store_true", help="Do not pass --memory to task containers")
    parser.add_argument("--docker-platform", default=None, help="Optional platform, e.g. linux/amd64")
    parser.add_argument("--force-restart", action="store_true", help="Pass --force-restart to src.main and ignore skip checks")
    parser.add_argument("--redo-eval", action="store_true", help="Evaluate again if patch exists but eval_result is missing")
    parser.add_argument("--eval-only", action="store_true", help="Only evaluate existing patch.diff artifacts; never regenerate patches")
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep pulled task images and disable docker prune cleanup after each task",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.workdir = args.workdir.resolve()
    args.state_file = args.state_file.resolve()
    run = RunContext(run_id=args.run_id or build_run_id())
    docker_available()
    eval_module = load_eval_module()

    tasks, manifest = expand_manifest(args.manifest.resolve(), args.workdir)
    if not tasks:
        print("No tasks selected.")
        return 0

    manifest_workers = manifest.get("max_workers") if isinstance(manifest, dict) else None
    if args.max_workers is not None:
        workers = args.max_workers
    elif manifest_workers is not None:
        workers = int(manifest_workers)
    else:
        workers = auto_workers(args.per_task_gb, args.reserve_gb, args.hard_cap_workers)
    workers = max(1, min(workers, len(tasks)))

    base_env = os.environ.copy()
    base_env.update(parse_env_file(REPO_ROOT / ".env"))
    dockerhub_users = [args.dockerhub_user, *args.fallback_dockerhub_user]
    dockerhub_users = [u for u in dict.fromkeys(dockerhub_users) if u]
    memory_gb = None if args.no_memory_limit else args.per_task_gb

    print(f"[plan] tasks={len(tasks)} workers={workers} per_task_gb={memory_gb or 'unlimited'}")
    print(f"[plan] state={args.state_file}")
    print(f"[plan] run_id={run.run_id}")

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_task,
                task,
                base_env=base_env,
                dockerhub_users=dockerhub_users,
                memory_gb=memory_gb,
                platform=args.docker_platform,
                force_restart=args.force_restart,
                redo_eval=args.redo_eval,
                prune=not args.no_prune,
                state_file=args.state_file,
                eval_module=eval_module,
                run=run,
                eval_only=args.eval_only,
            )
            for task in tasks
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"[{result['status']}] {result.get('output_subdir')} "
                f"{result.get('issue')} resolved={result.get('resolved', '-')}"
            )

    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    print(f"[summary] {counts}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
