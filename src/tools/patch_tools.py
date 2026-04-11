"""
MCP tools for the patch pipeline:

1. submit_patch_plan   — Patch Planner persists a structured PatchPlan into
                         SharedWorkingMemory.
2. apply_search_replace — Patch Generator applies SEARCH/REPLACE edits to
                          files in the target repository.

State is shared with ingestion_tools via its accessor functions — no
duplicate module-level state.
"""

from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from src.models.patch import FileEditPlan, PatchPlan
from src.tools.ingestion_tools import (
    _normalize_path,
    get_working_memory,
)


# ── submit_patch_plan ───────────────────────────────────────────────────

_SUBMIT_PATCH_PLAN_SCHEMA = {
    "type": "object",
    "description": (
        "Submit a structured patch plan produced by the Patch Planner agent. "
        "The plan is validated and stored in SharedWorkingMemory for the "
        "Patch Generator to consume."
    ),
    "required": ["overview", "edits"],
    "properties": {
        "overview": {
            "type": "string",
            "description": (
                "High-level summary of the fix strategy: root cause, approach, "
                "and how it respects constraints."
            ),
        },
        "edits": {
            "type": "array",
            "description": "Ordered list of per-file edit plans.",
            "items": {
                "type": "object",
                "required": ["filepath", "change_rationale"],
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the file, relative to repo root.",
                    },
                    "target_functions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Functions/methods/classes to modify or add in this file."
                        ),
                    },
                    "change_rationale": {
                        "type": "string",
                        "description": (
                            "Why this file needs to change, referencing evidence cards."
                        ),
                    },
                    "co_edit_dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Other filepaths that must be edited together with this file."
                        ),
                    },
                },
            },
        },
    },
}


@tool(
    "submit_patch_plan",
    (
        "Persist the structured patch plan into shared working memory. "
        "The Patch Planner MUST call this tool exactly once with the "
        "complete plan before finishing."
    ),
    _SUBMIT_PATCH_PLAN_SCHEMA,
)
async def submit_patch_plan(args: dict[str, Any]) -> dict[str, Any]:
    """Validate and store a PatchPlan in SharedWorkingMemory."""
    wm = get_working_memory()
    if wm is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No working memory initialized.",
                }
            ]
        }

    # Build per-file edit plans with path normalization
    edits: list[FileEditPlan] = []
    for raw_edit in args.get("edits", []):
        edits.append(
            FileEditPlan(
                filepath=_normalize_path(raw_edit["filepath"]),
                target_functions=raw_edit.get("target_functions", []),
                change_rationale=raw_edit["change_rationale"],
                co_edit_dependencies=[
                    _normalize_path(p)
                    for p in raw_edit.get("co_edit_dependencies", [])
                ],
            )
        )

    if not edits:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: Patch plan must contain at least one file edit.",
                }
            ]
        }

    plan = PatchPlan(
        overview=args["overview"],
        edits=edits,
    )

    wm.patch_plan = plan
    wm.record_action(
        f"submit_patch_plan: {len(edits)} file(s) — "
        + ", ".join(e.filepath for e in edits)
    )

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Patch plan stored: {len(edits)} file edit(s). "
                    f"Files: {', '.join(e.filepath for e in edits)}."
                ),
            }
        ]
    }


# ── apply_search_replace ────────────────────────────────────────────────

_APPLY_SEARCH_REPLACE_SCHEMA = {
    "type": "object",
    "description": (
        "Apply one or more SEARCH/REPLACE blocks to a single file. "
        "Each block finds an exact substring and replaces it. The SEARCH "
        "text must appear exactly once in the file."
    ),
    "required": ["filepath", "blocks"],
    "properties": {
        "filepath": {
            "type": "string",
            "description": "Path to the target file, relative to repo root.",
        },
        "blocks": {
            "type": "string",
            "description": (
                "One or more SEARCH/REPLACE blocks in the format:\n"
                "<<<<\n"
                "[exact old code to find]\n"
                "====\n"
                "[new code to replace it with]\n"
                ">>>>\n"
                "\n"
                "Multiple blocks are applied sequentially to the same file."
            ),
        },
    },
}


def _parse_search_replace_blocks(raw: str) -> list[tuple[str, str]]:
    """Parse <<<< / ==== / >>>> delimited blocks.

    Returns a list of (search, replace) tuples.
    """
    blocks: list[tuple[str, str]] = []
    remaining = raw
    while "<<<<" in remaining:
        start = remaining.index("<<<<")
        remaining = remaining[start + 4 :]

        if "====" not in remaining:
            raise ValueError(
                "Malformed SEARCH/REPLACE block: missing '====' separator."
            )
        sep = remaining.index("====")
        search_text = remaining[:sep].strip("\n")

        remaining = remaining[sep + 4 :]

        if ">>>>" not in remaining:
            raise ValueError(
                "Malformed SEARCH/REPLACE block: missing '>>>>' terminator."
            )
        end = remaining.index(">>>>")
        replace_text = remaining[:end].strip("\n")

        remaining = remaining[end + 4 :]

        if not search_text:
            raise ValueError("SEARCH block is empty — nothing to find.")

        blocks.append((search_text, replace_text))

    return blocks


@tool(
    "apply_search_replace",
    (
        "Apply exact SEARCH/REPLACE edits to a file in the target repository. "
        "The Patch Generator calls this tool for each file it needs to modify."
    ),
    _APPLY_SEARCH_REPLACE_SCHEMA,
)
async def apply_search_replace(args: dict[str, Any]) -> dict[str, Any]:
    """Parse SEARCH/REPLACE blocks and apply them to the target file."""
    from src.tools.ingestion_tools import _repo_root as repo_root_str

    wm = get_working_memory()
    if wm is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No working memory initialized.",
                }
            ]
        }

    raw_filepath = _normalize_path(args["filepath"])
    raw_blocks = args["blocks"]

    # Resolve against repo root
    if repo_root_str:
        # _repo_root ends with '/' and is forward-slash normalized
        abs_path = Path(repo_root_str.rstrip("/")) / raw_filepath
    else:
        abs_path = Path(raw_filepath)

    if not abs_path.is_file():
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"ERROR: File not found: {raw_filepath} (resolved to {abs_path})",
                }
            ]
        }

    # Parse blocks
    try:
        blocks = _parse_search_replace_blocks(raw_blocks)
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: {exc}"}]
        }

    if not blocks:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No SEARCH/REPLACE blocks found in input.",
                }
            ]
        }

    # Read file content
    content = abs_path.read_text(encoding="utf-8")

    # Apply blocks sequentially
    applied: list[str] = []
    for i, (search, replace) in enumerate(blocks, 1):
        count = content.count(search)
        if count == 0:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"ERROR in block {i}/{len(blocks)}: "
                            f"SEARCH text not found in {raw_filepath}.\n"
                            f"SEARCH text (first 200 chars):\n"
                            f"{search[:200]}"
                        ),
                    }
                ]
            }
        if count > 1:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"ERROR in block {i}/{len(blocks)}: "
                            f"SEARCH text found {count} times in {raw_filepath} "
                            f"(must be unique). Add more surrounding context to "
                            f"the SEARCH block.\n"
                            f"SEARCH text (first 200 chars):\n"
                            f"{search[:200]}"
                        ),
                    }
                ]
            }
        content = content.replace(search, replace, 1)
        applied.append(f"block {i}: OK")

    # Write back
    abs_path.write_text(content, encoding="utf-8")

    wm.record_action(
        f"apply_search_replace: {raw_filepath} — {len(blocks)} block(s) applied"
    )

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Successfully applied {len(blocks)} SEARCH/REPLACE "
                    f"block(s) to {raw_filepath}."
                ),
            }
        ]
    }
