"""Claude-driven multi-turn agentic long-term-memory retrieval.

Implements a MemGovern-style progressive Search -> Browse loop using the
Claude Agent SDK. Two MCP tools are exposed (``search_ltm_experiences`` and
``browse_ltm_experience``); the agent decides when to search, when to browse,
and which ids to return.

Failures (SDK error, structured-output retries exhausted, etc.) are raised to
the caller; ``_progressive_retrieve_ltm`` in the orchestrator records them in
``ltm_recommendations.json``. There is intentionally no HTTP fallback path —
if the SDK call cannot succeed, we want to see the failure, not paper over it.

Model selection follows the rest of the codebase: the SDK reads
``ANTHROPIC_MODEL`` from the environment (loaded by ``src.config``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import src.config  # noqa: F401  — side-effect: load .env into os.environ

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
)
from pydantic import BaseModel, Field

from src.memory import Experience, browse_experience
from src.tools.ltm_tools import browse_ltm_experience, search_ltm_experiences


class AgenticRetrievalResult(BaseModel):
    """Structured output of the LTM retrieval agent."""

    search_summaries: list[str] = Field(default_factory=list)
    selected_ids: list[str] = Field(default_factory=list)
    selected_scores: dict[str, float] = Field(
        default_factory=dict,
        description="Map from id to its ChromaDB score as returned by search_ltm_experiences.",
    )
    rationale: str = Field(default="")


LTM_AGENT_SYSTEM_PROMPT = """\
You are a Long-Term-Memory Retrieval Agent implementing MemGovern-style
progressive agentic search.

You have exactly two memory primitives:
1. search_ltm_experiences(query, top_k): returns outer-layer summary cards
2. browse_ltm_experience(id): opens one selected card for inner-layer detail

Required behavior:
- Start with search, not browse.
- Read summary cards, then selectively browse only the most promising ids.
- If the first search is weak, refine keywords and search again.
- You may perform multiple search/browse rounds.
- Browse at most 3 ids total unless results are clearly empty/noisy.
- Prefer diverse, high-signal experiences over near-duplicates.
- Base your final selection on analogical relevance to the current bug.

Output contract:
- search_summaries: concise bullet-style strings summarizing the most useful
  summary-layer hits you encountered across all searches
- selected_ids: final ids whose details should be injected downstream
- selected_scores: map from each selected id to its numeric score from the
  search results (the "score=X.XXXX" value shown in the search output)
- rationale: short explanation of why these experiences were selected

If nothing useful is found, return empty selected_ids and explain why.
"""


async def run_agentic_ltm_retrieval(
    *,
    stage: str,
    query_text: str,
    output_dir: Path,
    max_turns: int = 12,
    max_budget_usd: float = 1.0,
) -> tuple[list[str], list[str], list[Experience]]:
    """Run a multi-turn Search->Browse loop using Claude SDK tools.

    Raises:
        RuntimeError: if the SDK returns no ResultMessage, hits a per-query
            limit, exhausts structured-output retries, or yields no
            structured_output.
    """
    if not query_text.strip():
        return [], [], []

    ltm_mcp = create_sdk_mcp_server(
        name="ltm",
        tools=[search_ltm_experiences, browse_ltm_experience],
    )
    options = ClaudeAgentOptions(
        system_prompt=LTM_AGENT_SYSTEM_PROMPT,
        mcp_servers={"ltm": ltm_mcp},
        allowed_tools=[
            "mcp__ltm__search_ltm_experiences",
            "mcp__ltm__browse_ltm_experience",
        ],
        permission_mode="acceptEdits",
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format={
            "type": "json_schema",
            "schema": AgenticRetrievalResult.model_json_schema(),
        },
    )

    prompt = (
        f"Retrieval stage: {stage}\n\n"
        "Current bug / task context:\n"
        f"{query_text}\n\n"
        "Find prior experiences that are analogically useful for this stage. "
        "Search progressively, browse selectively, and return the final ids "
        "worth injecting downstream."
    )

    result_message: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_message = message

    if result_message is None:
        raise RuntimeError("ltm-agent: SDK returned no ResultMessage.")
    if result_message.subtype in ("error_max_turns", "error_max_budget_usd"):
        raise RuntimeError(
            f"ltm-agent: aborted due to per-query limit ({result_message.subtype})."
        )
    if result_message.subtype == "error_max_structured_output_retries":
        raise RuntimeError("ltm-agent: SDK exhausted structured-output retries.")
    if result_message.structured_output is None:
        raise RuntimeError(
            f"ltm-agent: missing structured_output (subtype={result_message.subtype})."
        )

    parsed = AgenticRetrievalResult.model_validate(result_message.structured_output)

    details: list[Experience] = []
    seen: set[str] = set()
    for unique_id in parsed.selected_ids:
        if unique_id in seen:
            continue
        seen.add(unique_id)
        detail = browse_experience(unique_id)
        if detail is None:
            continue
        details.append(
            Experience(
                id=detail.id,
                score=parsed.selected_scores.get(unique_id, 0.0),
                title=detail.title,
                symptom=detail.symptom,
                guidance=detail.guidance,
            )
        )

    summaries = [item.strip() for item in parsed.search_summaries if item.strip()]
    return summaries, parsed.selected_ids, details


def run_agentic_ltm_retrieval_sync(**kwargs):
    """Synchronous wrapper for orchestrator code paths."""
    return asyncio.run(run_agentic_ltm_retrieval(**kwargs))
