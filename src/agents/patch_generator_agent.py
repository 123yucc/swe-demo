"""
Patch Generator sub-agent: reads the PatchPlan from SharedWorkingMemory,
reads the target source files, and produces SEARCH/REPLACE edits that are
applied via the apply_search_replace MCP tool.

This agent is invoked directly by the orchestrator (code-driven pipeline)
rather than via the Agent tool dispatch.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)

import src.config  # noqa: F401  — side-effect: load .env into os.environ
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan
from src.tools.patch_tools import apply_search_replace


def _safe_preview(text: str, limit: int = 1000) -> str:
    """Return a console-safe preview string for Windows GBK terminals."""
    preview = text[:limit].replace("\n", " | ")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return preview.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _run_git_diff(repo_dir: Path, planned_files: list[str]) -> str:
    """Collect git diff for the planned files only.

    Mirrors the orchestrator's post-run verification intent, but keeps the
    patch-generator honest before it reports success. Untracked planned files
    are surfaced via `git add -N` and reset afterwards.
    """
    existing = [p for p in planned_files if (repo_dir / p).exists()]
    added: list[str] = []
    if existing:
        add_result = subprocess.run(
            ["git", "-C", str(repo_dir), "add", "-N", "-f", "--", *existing],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if add_result.returncode == 0:
            added = existing
        else:
            print(
                "[patch-generator] git add -N failed before diff: "
                f"{add_result.stderr.strip()}",
                flush=True,
            )

    diff_result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "HEAD", "--", *planned_files],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if added:
        reset_result = subprocess.run(
            ["git", "-C", str(repo_dir), "reset", "--", *added],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if reset_result.returncode != 0:
            print(
                "[patch-generator] git reset failed after diff: "
                f"{reset_result.stderr.strip()}",
                flush=True,
            )

    if diff_result.returncode != 0:
        print(
            "[patch-generator] git diff failed during success verification: "
            f"{diff_result.stderr.strip()}",
            flush=True,
        )
        return ""
    return diff_result.stdout or ""


def _planned_files_present_in_diff(diff_text: str, planned_files: list[str]) -> list[str]:
    """Return planned files that are missing from the generated diff."""
    if not planned_files:
        return []
    diff_paths: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" b/", 1)
        if len(parts) == 2:
            diff_paths.add(parts[1].strip().replace("\\", "/"))
    missing: list[str] = []
    for path in planned_files:
        norm = path.replace("\\", "/")
        if norm not in diff_paths:
            missing.append(norm)
    return missing

PATCH_GENERATOR_SYSTEM_PROMPT = """\
You are a Patch Generator — a precise code editor that executes a PatchPlan.

You receive a PatchPlan with preserved_findings and the original evidence
requirements. Produce SEARCH/REPLACE edits.

For each FileEditPlan in order:
1. READ the target file before generating any SEARCH blocks
2. IDENTIFY exact code regions that need to change
3. CONSTRUCT SEARCH/REPLACE blocks:
   <<<<<<SEARCH
   [exact old code to find]
   ======SPLIT
   [new code to replace it with]
   >>>>>>REPLACE
4. CALL mcp__patch__apply_search_replace for each file
5. If ERROR: re-read the file, adjust, and retry

CRITICAL — preserved_findings hard constraints (phase 18.D):
The preserved_findings list contains verbatim prescriptive snippets from the
original evidence findings.  These are HARD CONSTRAINTS — your SEARCH/REPLACE
must match the exact code expressions shown.  Before submitting each edit,
verify that it satisfies every preserved_findings snippet for this file.

If a preserved_findings snippet appears to conflict with your edit, DO NOT
ignore it — re-read the file and adjust the implementation to satisfy the
constraint.  Preserved findings are authoritative over your own inference.

Examples of preserved_findings hard constraints:
- "`ttl || Date.now() + interval > max`" → the formula must appear exactly
- "correct comparison: (ttl || Date.now()) + interval > max" → use this formula

Rules:
- SEARCH text MUST be exact verbatim copy of current file content
- MINIMAL DIFF: change only what the plan requires
- Preserve existing indentation style
- Apply edits in dependency order
- preserved_findings are hard constraints: verify before submitting

After all files are patched, output: PATCH_APPLIED
If any file could not be patched: PATCH_INCOMPLETE
"""


def _is_likely_real_repo_file(repo_dir: Path, filepath: str) -> bool:
    """Best-effort filter for patch plan entries that are not real repo files."""
    normalized = filepath.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        return False
    pseudo_prefixes = (
        "existence/",
        "symbol/",
        "symbols/",
        "call_chain/",
        "callchain/",
        "trace/",
        "graph/",
    )
    if normalized.startswith(pseudo_prefixes):
        return False
    candidate = repo_dir / normalized
    if candidate.exists():
        return candidate.is_file()
    suffix = Path(normalized).suffix.lower()
    if not suffix:
        return False
    allowed_suffixes = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
        ".rb", ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp",
        ".rs", ".swift", ".scala", ".sql", ".sh", ".yml", ".yaml",
        ".json", ".toml", ".ini", ".cfg", ".md",
    }
    return suffix in allowed_suffixes


def _sanitize_patch_plan(memory: SharedWorkingMemory, repo_dir: Path) -> PatchPlan | None:
    """Drop pseudo/non-file edit targets before invoking the patch generator."""
    patch_plan = memory.patch_plan
    if patch_plan is None:
        return None
    kept: list[FileEditPlan] = []
    dropped: list[str] = []
    for edit in patch_plan.edits:
        if _is_likely_real_repo_file(repo_dir, edit.filepath):
            kept.append(edit)
        else:
            dropped.append(edit.filepath)
    if dropped:
        print(
            "[patch-generator] Dropping non-repo or pseudo planned files: "
            + ", ".join(dropped),
            flush=True,
        )
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"FILTERED_PLANNED_FILES:{','.join(dropped)}",
        )
    sanitized = PatchPlan(overview=patch_plan.overview, edits=kept)
    memory.patch_plan = sanitized
    return sanitized


def _build_requirement_section(memory: SharedWorkingMemory, edit: FileEditPlan) -> str:
    """Include only requirements likely relevant to the current file edit."""
    if not memory.evidence_cards or not memory.evidence_cards.requirements:
        return ""
    normalized = edit.filepath.replace("\\", "/")
    req_lines: list[str] = []
    for req in memory.evidence_cards.requirements:
        if req.verdict in ("AS_IS_COMPLIANT", "UNCHECKED"):
            continue
        haystacks: list[str] = [req.text, "\n".join(req.findings)]
        haystacks.extend(req.evidence_locations)
        if normalized not in "\n".join(haystacks):
            continue
        req_lines.append(
            f"### {req.id} ({req.origin})\n"
            f"{req.text}\n"
            f"verdict: {req.verdict}\n"
            f"findings: {req.findings}"
        )
    if not req_lines:
        return ""
    return "\n\n## Relevant Requirements (verbatim)\n" + "\n\n".join(req_lines)


def _build_single_edit_prompt(memory: SharedWorkingMemory, edit: FileEditPlan) -> str:
    """Construct a focused prompt for one file-level edit plan."""
    findings = "\n".join(f"- {item}" for item in edit.preserved_findings) or "- (none)"
    targets = ", ".join(edit.target_functions) or "(unspecified)"
    co_edits = ", ".join(edit.co_edit_dependencies) or "(none)"
    req_section = _build_requirement_section(memory, edit)
    return (
        "Execute the following single-file patch plan. Only patch the target file in this run.\n\n"
        f"Patch plan overview:\n{memory.patch_plan.overview if memory.patch_plan else ''}\n\n"
        "## Target File Edit\n"
        f"filepath: {edit.filepath}\n"
        f"target_functions: {targets}\n"
        f"change_rationale: {edit.change_rationale}\n"
        f"co_edit_dependencies: {co_edits}\n"
        "preserved_findings:\n"
        f"{findings}\n"
        f"{req_section}\n\n"
        "Instructions:\n"
        "- Read the target file first.\n"
        "- Apply minimal SEARCH/REPLACE edits only to this file.\n"
        "- Respect preserved_findings as hard constraints.\n"
        "- If this file cannot be patched, output PATCH_INCOMPLETE.\n"
        "- If the file is successfully patched, output PATCH_APPLIED.\n"
    )


async def _run_single_file_patch(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
) -> bool:
    """Run a focused patch-generator query for a single file."""
    prompt = _build_single_edit_prompt(memory, edit)
    planned_files = [edit.filepath]
    patch_actions_before = sum(
        1
        for event in memory.action_history
        if event.phase == "patch-generation"
        and event.subagent == "apply_search_replace"
        and event.outcome.endswith(f":{edit.filepath}")
    )

    print(
        f"[patch-generator] Single-file prompt for {edit.filepath}: {len(prompt)} chars",
        flush=True,
    )

    patch_mcp = create_sdk_mcp_server(
        name="patch",
        version="1.0.0",
        tools=[apply_search_replace],
    )

    options = ClaudeAgentOptions(
        system_prompt=PATCH_GENERATOR_SYSTEM_PROMPT,
        allowed_tools=["Read", "mcp__patch__apply_search_replace", "TodoWrite"],
        mcp_servers={"patch": patch_mcp},
        cwd=str(repo_dir),
        max_turns=20,
        max_budget_usd=0.75,
        permission_mode="acceptEdits",
    )

    result_text = ""
    limit_hit: str | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                if message.subtype in ("error_max_turns", "error_max_budget_usd"):
                    limit_hit = message.subtype
                print(
                    f"[patch-generator] Received result for {edit.filepath} (subtype={message.subtype})",
                    flush=True,
                )

    if limit_hit is not None:
        print(
            f"[patch-generator] aborted for {edit.filepath} due to per-query limit: {limit_hit}",
            flush=True,
        )
        return False

    result_preview = _safe_preview(result_text)
    print(
        f"[patch-generator] Final result for {edit.filepath} (first 1000 chars): {result_preview}",
        flush=True,
    )

    has_patch_applied = "PATCH_APPLIED" in result_text
    has_patch_incomplete = "PATCH_INCOMPLETE" in result_text
    has_error = "ERROR" in result_text

    patch_actions_after = sum(
        1
        for event in memory.action_history
        if event.phase == "patch-generation"
        and event.subagent == "apply_search_replace"
        and event.outcome.endswith(f":{edit.filepath}")
    )
    applied_tool_calls = patch_actions_after - patch_actions_before
    diff_text = _run_git_diff(repo_dir, planned_files)
    missing_from_diff = _planned_files_present_in_diff(diff_text, planned_files)

    print(
        "[patch-generator] Verification: "
        f"file={edit.filepath}, tool_calls_delta={applied_tool_calls}, missing_from_diff={missing_from_diff}",
        flush=True,
    )

    if has_patch_applied and not missing_from_diff:
        if applied_tool_calls <= 0:
            print(
                f"[patch-generator] PATCH_APPLIED for {edit.filepath} but no apply_search_replace action was captured; trusting git diff verification",
                flush=True,
            )
        return True
    if has_patch_incomplete or has_error:
        return False
    return False


async def _run_patch_generator_async(
    memory: SharedWorkingMemory,
    repo_dir: Path,
) -> bool:
    """Run the patch generator agent. Returns True if all patches applied."""
    sanitized_plan = _sanitize_patch_plan(memory, repo_dir)
    if sanitized_plan is None or not sanitized_plan.edits:
        print("[patch-generator] No valid planned files remain after sanitization", flush=True)
        return False

    print(
        f"[patch-generator] Running focused patch generation for {len(sanitized_plan.edits)} files",
        flush=True,
    )
    for edit in sanitized_plan.edits:
        print(
            f"  - {edit.filepath} ({len(edit.preserved_findings)} preserved_findings)",
            flush=True,
        )

    all_succeeded = True
    for edit in sanitized_plan.edits:
        ok = await _run_single_file_patch(memory, repo_dir, edit)
        if ok:
            memory.record_action(
                phase="patch-generation",
                subagent="patch-generator",
                outcome=f"PATCH_SUCCESS:{edit.filepath}",
            )
            continue
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"PATCH_FAILED:{edit.filepath}",
        )
        all_succeeded = False
        break

    return all_succeeded


def run_patch_generator(memory: SharedWorkingMemory, repo_dir: Path) -> bool:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with patch plan and cached code.
        repo_dir: Repository root directory.

    Returns:
        True if patches were successfully applied.
    """
    return asyncio.run(_run_patch_generator_async(memory, repo_dir))
