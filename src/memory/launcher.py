"""
Auto-launcher for the MemGovern experience_server subprocess.

The pipeline treats long-term memory as a hard dependency: ``ensure_running``
will refuse to return until the server's /health responds. The Qwen
embedding model takes 30s+ to load on first run (and may need to download
weights from HuggingFace), so callers should expect a long initial wait.

The subprocess inherits stdio so loading progress is visible to the user.
On clean Python exit, atexit terminates the subprocess we launched. Pre-existing
servers (started by hand, e.g. for debugging) are left untouched.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
from pathlib import Path

from src.memory.retrieve import health_check, wait_until_ready


DEFAULT_DATA_DIR = Path("workdir") / "long_term_memory"
DEFAULT_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_PORT = 9030


_launched_proc: subprocess.Popen | None = None


def _validate_data_dir(data_dir: Path) -> None:
    """Fail loudly if the long-term memory data is not in place."""
    json_path = data_dir / "experience_data.json"
    chroma_path = data_dir / "chroma_db_experience"
    missing: list[str] = []
    if not json_path.exists():
        missing.append(str(json_path))
    if not chroma_path.exists():
        missing.append(str(chroma_path))
    if missing:
        raise FileNotFoundError(
            "Long-term memory data not found. Required files:\n  "
            + "\n  ".join(missing)
            + "\n\nDownload from QuantaAlpha/MemGovern (Git LFS):\n"
              "  data/agentic_exp_data_1220_13w_DSnewPrompt/experience_data.json\n"
              "  data/agentic_exp_data_1220_13w_DSnewPrompt/chroma_db_experience/\n"
              f"and place them under {data_dir}."
        )


def _terminate_launched() -> None:
    global _launched_proc
    if _launched_proc is None:
        return
    if _launched_proc.poll() is None:
        try:
            _launched_proc.terminate()
            _launched_proc.wait(timeout=5)
        except Exception:
            try:
                _launched_proc.kill()
            except Exception:
                pass
    _launched_proc = None


def ensure_running(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    model_path: str = DEFAULT_MODEL_ID,
    port: int = DEFAULT_PORT,
    max_wait_sec: int = 600,
) -> bool:
    """Start the experience_server subprocess if it is not already running.

    Returns True if the server is up and /health passes.

    If a server already responds on the configured port, returns immediately
    without launching a new one (this is the dev workflow: start the server
    in a separate terminal, then run the harness repeatedly).
    """
    global _launched_proc

    # Already running? Nothing to do.
    if health_check(timeout=3):
        print(f"[ltm] experience_server already running on port {port}", flush=True)
        return True

    data_dir = Path(data_dir).resolve()
    _validate_data_dir(data_dir)

    server_script = Path(__file__).resolve().parents[2] / "tools" / "experience_server.py"
    if not server_script.exists():
        raise FileNotFoundError(f"experience_server.py not found at {server_script}")

    env = os.environ.copy()
    env["DB_DIR"] = str(data_dir / "chroma_db_experience")
    env["JSON_DATA_PATH"] = str(data_dir / "experience_data.json")
    env["MODEL_PATH"] = model_path
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)

    print(
        f"[ltm] launching experience_server "
        f"(model={model_path}, data={data_dir}); "
        "first run downloads ~1.2GB and loads embeddings, may take 1-3 min",
        flush=True,
    )

    _launched_proc = subprocess.Popen(
        [sys.executable, str(server_script)],
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    atexit.register(_terminate_launched)

    print(f"[ltm] waiting for /health on port {port} (up to {max_wait_sec}s)...", flush=True)
    if not wait_until_ready(max_wait_sec=max_wait_sec):
        # Subprocess died or never came up; surface its exit code if any.
        rc = _launched_proc.poll()
        raise RuntimeError(
            f"experience_server failed to become ready within {max_wait_sec}s "
            f"(subprocess exit code: {rc})."
        )

    print("[ltm] experience_server ready.", flush=True)
    return True
