"""HTTP client for the MemGovern experience_server.

This module exposes the same two primitives used by MemGovern's retrieval
workflow:

* ``search_ids`` / ``search_experiences``: search by free-text query and only
  return the outer summary layer (search hits / bug descriptions)
* ``fetch_detail`` / ``browse_experience``: open one selected experience and
  return its inner fix guidance

The retrieval flow is progressive: search summaries first (``search_experiences``),
then browse details selectively (``browse_experience``).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


SERVER_HOST = os.environ.get("MEMGOVERN_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("MEMGOVERN_PORT", "9030"))
SEARCH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/search"
GET_EXPERIENCE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/get_experience"
HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/health"

DEFAULT_TIMEOUT_SEC = 30

# Default path for disabled IDs; override with env var LTM_DISABLED_IDS_PATH
_DEFAULT_DISABLED_IDS_PATH = Path("workdir/long_term_memory/disabled_ids.json")


def _load_disabled_ids() -> set[str]:
    """Load disabled experience IDs from the disabled_ids.json file."""
    path_str = os.environ.get("LTM_DISABLED_IDS_PATH", "")
    path = Path(path_str) if path_str else _DEFAULT_DISABLED_IDS_PATH
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("disabled", []))
    except (json.JSONDecodeError, OSError):
        return set()


class Experience(BaseModel):
    """One retrieved experience record, joined from /search + /get_experience."""

    id: str = Field(description="MemGovern unique id")
    score: float = Field(description="ChromaDB distance score (lower = closer)")
    title: str = Field(default="")
    symptom: str = Field(default="")
    guidance: str = Field(default="")


class ExperienceSearchHit(BaseModel):
    """Outer-layer summary returned by the search primitive."""

    id: str = Field(description="MemGovern unique id")
    score: float = Field(description="ChromaDB distance score (lower = closer)")
    symptom: str = Field(default="", description="Summary / bug description preview")


class ExperienceDetail(BaseModel):
    """Inner-layer detail returned by the browse primitive."""

    id: str = Field(description="MemGovern unique id")
    title: str = Field(default="")
    symptom: str = Field(default="")
    guidance: str = Field(default="")


def _post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get_json(url: str, params: dict[str, str], timeout: int) -> dict[str, Any]:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        return json.load(resp)


def health_check(timeout: int = 5) -> dict[str, Any] | None:
    """Return the server's /health response, or None if unreachable."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def search_ids(query: str, top_k: int, timeout: int = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Call /search and return the raw result list."""
    resp = _post_json(SEARCH_URL, {"query": query, "top_k": top_k}, timeout=timeout)
    if not resp.get("success"):
        raise RuntimeError(f"experience_server /search failed: {resp.get('error')}")
    return list(resp.get("results") or [])


def fetch_detail(unique_id: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> dict[str, Any] | None:
    """Call /get_experience for one id; return the data dict or None on 404."""
    try:
        resp = _get_json(GET_EXPERIENCE_URL, {"id": unique_id}, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if not resp.get("success"):
        return None
    return resp.get("data") or {}


def search_experiences(
    query: str,
    top_k: int = 5,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> list[ExperienceSearchHit]:
    """Search only and return summary hits for progressive browsing.

    This mirrors MemGovern's ``exp_search`` primitive: the caller gets compact
    outer-layer summaries first and can decide which ids to open in detail.
    """
    if not query.strip():
        return []

    disabled = _load_disabled_ids()
    raw = search_ids(query, top_k=top_k, timeout=timeout)
    hits: list[ExperienceSearchHit] = []
    for hit in raw:
        unique_id = str(hit.get("id") or "")
        if not unique_id:
            continue
        if disabled and unique_id in disabled:
            continue
        symptom = (
            hit.get("bug_description")
            or hit.get("symptom")
            or hit.get("content_preview")
            or ""
        )
        score = float(hit.get("score") or 0.0)
        hits.append(ExperienceSearchHit(id=unique_id, score=score, symptom=symptom))
    return hits


def browse_experience(
    unique_id: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> ExperienceDetail | None:
    """Open one experience by id and return the detailed fix guidance."""
    if not unique_id.strip():
        return None

    detail = fetch_detail(unique_id, timeout=timeout)
    if not detail:
        return None
    return ExperienceDetail(
        id=str(detail.get("id") or unique_id),
        title=str(detail.get("title") or ""),
        symptom=str(detail.get("bug_description") or detail.get("symptom") or ""),
        guidance=str(detail.get("fix_experience") or detail.get("guidance") or ""),
    )


# ─────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────

def append_recommendations_log(
    output_dir: Path,
    *,
    stage: str,
    query: str,
    search_summaries: list[str] | None = None,
    selected_ids: list[str] | None = None,
    experiences: list[Experience],
    error: str = "",
) -> Path:
    """Append one retrieval record to ``ltm_recommendations.json``.

    The file is a JSON array of records; if it does not exist it is
    initialised with an empty array first.  Each record captures stage
    (under_specified / patch_planning / ...), the query text, summary-layer
    hits, selected ids, and any browsed experiences so analysts can
    correlate any downstream decision with what was injected.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "ltm_recommendations.json"

    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    else:
        existing = []

    record = {
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "search_summaries": list(search_summaries or []),
        "selected_ids": list(selected_ids or []),
        "experiences": [e.model_dump() for e in experiences],
        "error": error,
    }
    existing.append(record)
    log_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return log_path


def format_experiences_for_prompt(
    experiences: list[Experience],
    *,
    include_fix: bool,
) -> str:
    """Render retrieved experiences as a prompt-injectable text block.

    Always wraps the block in a hard-constraint preamble: the retrieved
    knowledge is REFERENCE only; if it conflicts with the current code,
    the code wins.
    """
    if not experiences:
        return ""

    parts: list[str] = [
        "═══ LONG-TERM MEMORY (REFERENCE EXPERIENCES) ═══",
        "",
        "The following are similar past bug fixes retrieved from a long-term",
        "memory store. Treat them as REFERENCE EXPERIENCE only — they describe",
        "historical bugs in other contexts, not the current case.",
        "",
        "HARD CONSTRAINT: if any reference here conflicts with what the actual",
        "code in the current repo shows, the code wins. Reference experience",
        "may not match the current code base, language, or framework.",
        "",
    ]
    for i, exp in enumerate(experiences, 1):
        parts.append(f"--- Reference {i} (score={exp.score:.4f}) ---")
        if exp.title:
            parts.append(f"Title: {exp.title.strip()}")
        if exp.symptom:
            parts.append("Symptom:")
            parts.append(exp.symptom.strip())
        if include_fix and exp.guidance:
            parts.append("")
            parts.append("Guidance:")
            parts.append(exp.guidance.strip())
        parts.append("")
    parts.append("═══ END LONG-TERM MEMORY ═══")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Server liveness
# ─────────────────────────────────────────────────────────────────────────

def wait_until_ready(max_wait_sec: int = 600, poll_interval: float = 2.0) -> bool:
    """Poll /health until the server reports ok, or until max_wait elapses.

    The Qwen embedding model takes 30–120s to load on first run (and longer
    if HuggingFace needs to download weights).  This blocks until ready.
    """
    start = time.monotonic()
    while time.monotonic() - start < max_wait_sec:
        h = health_check(timeout=3)
        if h and h.get("status") == "ok":
            return True
        time.sleep(poll_interval)
    return False


# ─────────────────────────────────────────────────────────────────────────
# Custom-rule library logging (independent of ChromaDB path)
# ─────────────────────────────────────────────────────────────────────────

def append_custom_recommendations_log(
    output_dir: Path,
    *,
    stage: str,
    query: str,
    route: dict | None,
    matched_ids: list[str] | None = None,
    error: str = "",
) -> Path:
    """Append one custom-rule routing record to ``custom_recommendations.json``.

    Mirrors the shape of ``append_recommendations_log`` but logs router
    output instead of ChromaDB search results. Kept in a separate file
    so analysts can correlate each route call with which rules ended up
    injected, without confusing the two retrieval paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "custom_recommendations.json"

    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    else:
        existing = []

    record = {
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "route": route or {},
        "matched_ids": list(matched_ids or []),
        "error": error,
    }
    existing.append(record)
    log_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return log_path
