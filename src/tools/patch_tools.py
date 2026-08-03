"""
MCP tools for the patch pipeline.

Patch Planner returns PatchPlan via SDK structured output. This module only
exposes the edit-application tool consumed by Patch Generator.
"""

from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from src.tools.ingestion_tools import (
    _normalize_path,
    get_working_memory,
)
from src.orchestrator.repo_executor import run_repo_command


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
                "<<<<<<SEARCH\n"
                "[exact old code to find]\n"
                "======SPLIT\n"
                "[new code to replace it with]\n"
                ">>>>>>REPLACE\n"
                "\n"
                "Multiple blocks are applied sequentially to the same file."
            ),
        },
    },
}

_CREATE_FILE_SCHEMA = {
    "type": "object",
    "description": (
        "Create a new file in the target repository with exact full content. "
        "Use only for PatchPlan entries marked as creating a new file."
    ),
    "required": ["filepath", "content"],
    "properties": {
        "filepath": {
            "type": "string",
            "description": "Path to the new file, relative to repo root.",
        },
        "content": {
            "type": "string",
            "description": "Complete file content to write.",
        },
        "overwrite": {
            "type": "boolean",
            "description": (
                "Whether to overwrite an existing file. Defaults to false; "
                "patch generation should normally not overwrite."
            ),
        },
    },
}


_SEARCH_SEP = "<<<<<<SEARCH"
_SPLIT_SEP = "======SPLIT"
_REPLACE_SEP = ">>>>>>REPLACE"


def _parse_search_replace_blocks(raw: str) -> list[tuple[str, str]]:
    """Parse <<<<<<SEARCH / ======SPLIT / >>>>>>REPLACE delimited blocks.

    Returns a list of (search, replace) tuples.

    The longer, annotated delimiters avoid false matches on code content
    such as ``====`` or ``>>>>`` that commonly appear in test files.
    """
    blocks: list[tuple[str, str]] = []
    remaining = raw
    while _SEARCH_SEP in remaining:
        start = remaining.index(_SEARCH_SEP)
        remaining = remaining[start + len(_SEARCH_SEP) :]

        if _SPLIT_SEP not in remaining:
            raise ValueError(
                f"Malformed SEARCH/REPLACE block: missing '{_SPLIT_SEP}' separator."
            )
        sep = remaining.index(_SPLIT_SEP)
        search_text = remaining[:sep].strip("\n")

        remaining = remaining[sep + len(_SPLIT_SEP) :]

        if _REPLACE_SEP not in remaining:
            raise ValueError(
                f"Malformed SEARCH/REPLACE block: missing '{_REPLACE_SEP}' terminator."
            )
        end = remaining.index(_REPLACE_SEP)
        replace_text = remaining[:end].strip("\n")

        remaining = remaining[end + len(_REPLACE_SEP) :]

        if not search_text:
            raise ValueError("SEARCH block is empty; nothing to find.")

        blocks.append((search_text, replace_text))

    return blocks


def _validate_syntax(path: Path, repo_dir: Path) -> str:
    """Run a language-appropriate syntax check on *path*.

    Returns an empty string on success, or an error message on failure.
    Files with unsupported extensions are skipped (return "").
    """
    suffix = path.suffix.lower()
    try:
        rel_path = str(path.relative_to(repo_dir)).replace("\\", "/")
    except ValueError:
        rel_path = str(path)
    if suffix == ".py":
        returncode, output, _ = run_repo_command(
            ["python", "-m", "py_compile", rel_path],
            repo_dir=repo_dir,
            timeout=120,
        )
        if returncode == 127:
            return ""
        if returncode != 0:
            return output.strip() or f"py_compile exited with code {returncode}"
    elif suffix in (".js", ".mjs", ".cjs"):
        returncode, output, _ = run_repo_command(
            ["node", "--check", rel_path],
            repo_dir=repo_dir,
            timeout=120,
        )
        if returncode == 127:
            return ""
        if returncode != 0:
            return output.strip() or f"node --check exited with code {returncode}"
    # `node --check` cannot parse TypeScript and reports
    # ERR_UNKNOWN_FILE_EXTENSION for otherwise valid .ts/.tsx files. Project
    # TypeScript configurations also cannot be reproduced reliably by invoking
    # `tsc` on one file in isolation, so defer these files to the later
    # repository-aware build/static verification stage.
    return ""


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

    # Resolve against repo root.
    if repo_root_str:
        # _repo_root ends with '/' and is forward-slash normalized.
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

    try:
        blocks = _parse_search_replace_blocks(raw_blocks)
    except ValueError as exc:
        return {"content": [{"type": "text", "text": f"ERROR: {exc}"}]}

    if not blocks:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "ERROR: No SEARCH/REPLACE blocks found in input.",
                }
            ]
        }

    original_content = abs_path.read_text(encoding="utf-8")
    content = original_content

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

    abs_path.write_text(content, encoding="utf-8")

    repo_dir = Path(repo_root_str.rstrip("/")) if repo_root_str else abs_path.parent
    syntax_error = _validate_syntax(abs_path, repo_dir)
    if syntax_error:
        abs_path.write_text(original_content, encoding="utf-8")
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"ERROR: Syntax validation failed after applying edits to "
                        f"{raw_filepath}. File has been rolled back to its original "
                        f"state. Fix the REPLACE blocks and retry.\n"
                        f"Syntax error: {syntax_error}"
                    ),
                }
            ]
        }

    wm.record_action(
        phase="patch-generation",
        subagent="apply_search_replace",
        outcome=f"{len(blocks)}_blocks_applied:{raw_filepath}",
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


@tool(
    "create_file",
    (
        "Create a new file in the target repository with exact full content. "
        "The Patch Generator calls this tool only for planned new-file edits."
    ),
    _CREATE_FILE_SCHEMA,
)
async def create_file(args: dict[str, Any]) -> dict[str, Any]:
    """Create a planned new file and run syntax validation when possible."""
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
    content = str(args.get("content") or "")
    overwrite = bool(args.get("overwrite") or False)

    if repo_root_str:
        repo_dir = Path(repo_root_str.rstrip("/"))
        abs_path = repo_dir / raw_filepath
    else:
        abs_path = Path(raw_filepath)
        repo_dir = abs_path.parent

    try:
        abs_path.relative_to(repo_dir)
    except ValueError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"ERROR: Refusing to create file outside repo: {raw_filepath}",
                }
            ]
        }

    if abs_path.exists() and not overwrite:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"ERROR: File already exists: {raw_filepath}",
                }
            ]
        }

    if not content:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"ERROR: Refusing to create empty file: {raw_filepath}",
                }
            ]
        }

    original_content: str | None = None
    if abs_path.exists():
        original_content = abs_path.read_text(encoding="utf-8", errors="replace")

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")

    syntax_error = _validate_syntax(abs_path, repo_dir)
    if syntax_error:
        if original_content is None:
            try:
                abs_path.unlink()
            except OSError:
                pass
        else:
            abs_path.write_text(original_content, encoding="utf-8")
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"ERROR: Syntax validation failed after creating "
                        f"{raw_filepath}. File has been rolled back.\n"
                        f"Syntax error: {syntax_error}"
                    ),
                }
            ]
        }

    wm.record_action(
        phase="patch-generation",
        subagent="create_file",
        outcome=f"file_created:{raw_filepath}",
    )

    return {
        "content": [
            {
                "type": "text",
                "text": f"Successfully created file {raw_filepath}.",
            }
        ]
    }
