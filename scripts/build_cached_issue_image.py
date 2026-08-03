#!/usr/bin/env python3
"""Build a local cached issue image that matches the runner's Docker tag.

This is a fallback when a specific SWE-bench image tag cannot be pulled from
Docker Hub, but the issue repo has already been extracted to workdir.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = REPO_ROOT / "workdir"


def normalize_issue(value: str) -> str:
    value = str(value).strip()
    if value.startswith("swe_issue_"):
        return value
    if value.isdigit():
        return f"swe_issue_{int(value):03d}"
    raise ValueError(f"Unsupported issue identifier: {value!r}")


def dockerhub_tag_from_image_name(image_name: str) -> str:
    if not image_name or ":" not in image_name:
        return ""
    return image_name.rsplit("/", 1)[-1].replace(":", "-")[:128]


def load_metadata(issue_dir: Path) -> dict:
    path = issue_dir / "artifacts" / "instance_metadata.json"
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_tag(metadata: dict) -> str:
    tag = str(metadata.get("dockerhub_tag") or "").strip()
    if tag:
        return tag
    return dockerhub_tag_from_image_name(str(metadata.get("image_name") or ""))


def write_build_files(
    repo_dir: Path,
    base_commit: str,
    base_image: str,
    *,
    prefetch_go_modules: bool,
) -> tuple[Path, Path]:
    dockerfile = repo_dir / ".codex_cached_image.Dockerfile"
    dockerignore = repo_dir / ".dockerignore"
    extra_setup = ""
    go_check_cmd = "go version"
    if base_image.startswith("golang:"):
        extra_setup = """RUN apt-get update && apt-get install -y \\
    git \\
    bash \\
    build-essential \\
    python3 \\
    python3-pip \\
    python3-setuptools \\
    python-is-python3 \\
    perl \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

"""
    else:
        extra_setup = """ENV GOROOT=/usr/local/go
ENV GOPATH=/go
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

"""
        go_check_cmd = "/usr/local/go/bin/go version"
    go_prefetch = ""
    if prefetch_go_modules:
        go_prefetch = "RUN go mod download\n"

    dockerfile.write_text(
        f"""FROM {base_image}

{extra_setup}RUN /bin/bash -lc 'command -v git >/dev/null && command -v python3 >/dev/null'

WORKDIR /app
COPY . /app/
RUN git reset --hard {base_commit} && git clean -fdx && git checkout {base_commit}
RUN {go_check_cmd}
{go_prefetch}

ENTRYPOINT ["/bin/bash"]
""",
        encoding="utf-8",
    )
    dockerignore.write_text("", encoding="utf-8")
    return dockerfile, dockerignore


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def docker_build_command(
    *,
    full_tag: str,
    dockerfile: Path,
    prefetch_go_modules: bool,
) -> list[str]:
    cmd = ["docker", "build", "-t", full_tag, "-f", str(dockerfile)]
    if prefetch_go_modules:
        cmd.extend(["--network", "host"])
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
            value = os.environ.get(key)
            if value:
                cmd.extend(["--build-arg", f"{key}={value}"])
    cmd.append(".")
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", required=True, help="Issue number or swe_issue_NNN")
    parser.add_argument("--owner", default="jefzda", help="DockerHub owner prefix")
    parser.add_argument("--workdir", type=Path, default=WORKDIR)
    parser.add_argument(
        "--base-image",
        default="golang:1.20-bookworm",
        help="Base image used to construct the cached issue image",
    )
    parser.add_argument("--tag-only", action="store_true", help="Only print resolved tag and exit")
    args = parser.parse_args()

    issue_name = normalize_issue(args.issue)
    issue_dir = args.workdir / issue_name
    repo_dir = issue_dir / "repo"
    if not repo_dir.is_dir():
        raise SystemExit(f"Missing repo dir: {repo_dir}")

    metadata = load_metadata(issue_dir)
    dockerhub_tag = resolve_tag(metadata)
    if not dockerhub_tag:
        raise SystemExit("No dockerhub_tag/image_name-derived tag found in metadata")

    full_tag = f"{args.owner}/sweap-images:{dockerhub_tag}"
    print(full_tag)
    if args.tag_only:
        return

    base_commit = str(metadata.get("base_commit") or "").strip()
    if not base_commit:
        raise SystemExit("Missing base_commit in metadata")

    prefetch_go_modules = (repo_dir / "go.mod").is_file()

    dockerfile, dockerignore = write_build_files(
        repo_dir,
        base_commit,
        args.base_image,
        prefetch_go_modules=prefetch_go_modules,
    )
    try:
        run(
            docker_build_command(
                full_tag=full_tag,
                dockerfile=dockerfile,
                prefetch_go_modules=prefetch_go_modules,
            ),
            cwd=repo_dir,
        )
        run(["docker", "image", "inspect", full_tag])
    finally:
        dockerfile.unlink(missing_ok=True)
        dockerignore.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
