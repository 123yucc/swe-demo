"""In-process SDK MCP tools for MemGovern-style long-term-memory search/browse."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from src.memory import browse_experience, search_experiences


_SEARCH_LTM_SCHEMA = {
    "type": "object",
    "description": (
        "Search long-term memory by free-text query and return only compact "
        "outer-layer summary cards. Use this first before browsing details."
    ),
    "required": ["query"],
    "properties": {
        "query": {
            "type": "string",
            "description": "Free-text retrieval query extracted from the current bug.",
        },
        "top_k": {
            "type": "integer",
            "description": "Maximum number of summary hits to return.",
            "default": 5,
            "minimum": 1,
            "maximum": 20,
        },
    },
}


_BROWSE_LTM_SCHEMA = {
    "type": "object",
    "description": (
        "Browse one selected long-term-memory experience by id and return its "
        "inner-layer detailed guidance. Use only after search_ltm_experiences."
    ),
    "required": ["id"],
    "properties": {
        "id": {
            "type": "string",
            "description": "Experience id returned by search_ltm_experiences.",
        }
    },
}


@tool(
    "search_ltm_experiences",
    (
        "Search long-term memory using a free-text query. Returns only summary "
        "cards (id, score, symptom preview). Use this tool before browsing."
    ),
    _SEARCH_LTM_SCHEMA,
)
async def search_ltm_experiences(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "content": [{"type": "text", "text": "ERROR: query must be non-empty."}],
            "is_error": True,
        }

    top_k_raw = args.get("top_k", 5)
    try:
        top_k = max(1, min(int(top_k_raw), 20))
    except (TypeError, ValueError):
        top_k = 5

    try:
        hits = search_experiences(query=query, top_k=top_k)
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: search failed: {exc}"}],
            "is_error": True,
        }

    if not hits:
        return {
            "content": [{"type": "text", "text": "No matching long-term-memory experiences found."}]
        }

    lines = []
    for hit in hits:
        symptom = (hit.symptom or "").strip().replace("\n", " ")
        if len(symptom) > 280:
            symptom = symptom[:277] + "..."
        lines.append(f"id={hit.id} | score={hit.score:.4f} | symptom={symptom}")

    return {
        "content": [
            {
                "type": "text",
                "text": "Search results (summary layer only):\n" + "\n".join(lines),
            }
        ]
    }


@tool(
    "browse_ltm_experience",
    (
        "Browse a single long-term-memory experience by id and return its "
        "detailed bug description and fix guidance."
    ),
    _BROWSE_LTM_SCHEMA,
)
async def browse_ltm_experience(args: dict[str, Any]) -> dict[str, Any]:
    unique_id = str(args.get("id") or "").strip()
    if not unique_id:
        return {
            "content": [{"type": "text", "text": "ERROR: id must be non-empty."}],
            "is_error": True,
        }

    try:
        detail = browse_experience(unique_id)
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: browse failed: {exc}"}],
            "is_error": True,
        }

    if detail is None:
        return {
            "content": [{"type": "text", "text": f"No detail found for id={unique_id}."}],
            "is_error": True,
        }

    text = (
        f"id={detail.id}\n"
        f"title={detail.title}\n"
        f"symptom={detail.symptom}\n\n"
        f"guidance:\n{detail.guidance}"
    )
    return {"content": [{"type": "text", "text": text}]}