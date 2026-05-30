"""
Patch Generator sub-agent: reads the PatchPlan from SharedWorkingMemory,
reads the target source files, and produces SEARCH/REPLACE edits that are
applied via the apply_search_replace MCP tool.

This agent is invoked directly by the orchestrator (code-driven pipeline)
rather than via the Agent tool dispatch.
"""

import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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


AttemptOutcome = Literal["CHANGED", "IDEMPOTENT", "FAILED"]


_IDEMPOTENT_MARKERS = (
    "patch_applied",
    "already",
    "no change needed",
    "no changes needed",
    "no changes were needed",
    "no substantive changes",
    "correct state",
    "correct target state",
    "already in place",
    "already conformant",
)


def _classify_attempt(
    *,
    hash_changed: bool,
    tool_calls_delta: int,
    result_text: str,
) -> AttemptOutcome:
    """Decide whether a sub-edit attempt succeeded.

    Three outcomes:
    - CHANGED: file content hash differs before/after (real edit applied).
    - IDEMPOTENT: no hash change, but the model affirmed the file is already
      at the target state (PATCH_APPLIED / "already correct" / etc.). This
      is success — no retry, the patch plan was a no-op for this file.
    - FAILED: no hash change, no idempotency signal. Real silent failure;
      caller may retry.

    tool_calls_delta is recorded for diagnostics but is no longer the primary
    signal: a model that says "PATCH_APPLIED — already in target state"
    without calling apply_search_replace is correct, not failed.
    """
    if hash_changed:
        return "CHANGED"
    lowered = result_text.lower()
    if "patch_incomplete" in lowered:
        return "FAILED"
    for marker in _IDEMPOTENT_MARKERS:
        if marker in lowered:
            return "IDEMPOTENT"
    return "FAILED"


def _file_hash(path: Path) -> str | None:
    """Return sha1 of *path* contents, or None if the file doesn't exist."""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return None


class PatchFailureLogger:
    """Append-only JSONL log for patch-generator failures.

    Written to ``<output_dir>/patch_failures.log`` (jsonl, one event per line).
    Three event kinds:
    - ``attempt_failed``: a single sub-edit attempt classified as FAILED.
    - ``file_summary``: per-file rollup written after all sub-edits + fallback.
    - ``run_summary``: pipeline-level rollup written at the end of a run.

    The logger is best-effort: any IO error is swallowed with a stderr print
    so the patch pipeline itself never crashes because of logging.
    """

    def __init__(self, output_dir: Path | None) -> None:
        self._path: Path | None = None
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            self._path = output_dir / "patch_failures.log"

    def _write(self, payload: dict) -> None:
        if self._path is None:
            return
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[patch-generator] PatchFailureLogger write failed: {exc}", flush=True)

    def attempt_failed(
        self,
        *,
        filepath: str,
        sub_label: str,
        attempt_idx: int,
        prompt_chars: int,
        max_turns: int,
        max_budget: float,
        subtype: str,
        limit_hit: str | None,
        tool_calls_delta: int,
        hash_changed: bool,
        classification: AttemptOutcome,
        result_preview: str,
    ) -> None:
        self._write(
            {
                "kind": "attempt_failed",
                "file": filepath,
                "sub_label": sub_label,
                "attempt": attempt_idx,
                "prompt_chars": prompt_chars,
                "max_turns": max_turns,
                "max_budget": max_budget,
                "subtype": subtype,
                "limit_hit": limit_hit,
                "tool_calls_delta": tool_calls_delta,
                "hash_changed": hash_changed,
                "classification": classification,
                "result_preview": result_preview,
            }
        )

    def file_summary(
        self,
        *,
        filepath: str,
        sub_edits_total: int,
        sub_edits_changed: int,
        sub_edits_idempotent: int,
        sub_edits_failed: int,
        fallback_used: bool,
        fallback_outcome: AttemptOutcome | None,
        missing_from_diff: list[str],
        final_status: str,
    ) -> None:
        self._write(
            {
                "kind": "file_summary",
                "file": filepath,
                "sub_edits_total": sub_edits_total,
                "sub_edits_changed": sub_edits_changed,
                "sub_edits_idempotent": sub_edits_idempotent,
                "sub_edits_failed": sub_edits_failed,
                "fallback_used": fallback_used,
                "fallback_outcome": fallback_outcome,
                "missing_from_diff": missing_from_diff,
                "final_status": final_status,
            }
        )

    def run_summary(
        self,
        *,
        files_total: int,
        files_succeeded: int,
        files_failed: int,
    ) -> None:
        self._write(
            {
                "kind": "run_summary",
                "files_total": files_total,
                "files_succeeded": files_succeeded,
                "files_failed": files_failed,
            }
        )


def _safe_preview(text: str, limit: int = 1000) -> str:
    """Return a console-safe preview string for Windows GBK terminals."""
    preview = text[:limit].replace("\n", " | ")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return preview.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _strip_mode_only_hunks(diff_text: str) -> str:
    """Remove diff entries that contain only file-mode changes or submodule dirty markers."""
    if not diff_text:
        return diff_text
    entries: list[str] = []
    current_lines: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current_lines:
            entries.append("".join(current_lines))
            current_lines = []
        current_lines.append(line)
    if current_lines:
        entries.append("".join(current_lines))

    kept: list[str] = []
    for entry in entries:
        lines = entry.splitlines()
        has_content = any(
            (l.startswith("+") or l.startswith("-"))
            and not l.startswith("--- ")
            and not l.startswith("+++ ")
            and "Subproject commit " not in l
            for l in lines
        )
        if has_content:
            kept.append(entry)
    return "".join(kept)


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
    return _strip_mode_only_hunks(diff_result.stdout or "")


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


# Per-edit attempt count. Each subsequent attempt bumps max_turns and
# budget so an edit silently truncated by turn-exhaustion gets a real
# chance to finish. SWE-bench Pro requires all tests to pass, so a single
# unfinished edit fails the whole case — give as much rope as needed.
_SUB_EDIT_MAX_ATTEMPTS = 3
# (max_turns, max_budget_usd) per attempt index (1-based, padded to last).
_ATTEMPT_LIMITS = [(20, 0.75), (30, 1.2), (40, 1.8)]

# Soft threshold: if the planner emits a single FileEditPlan with this many
# functions or findings, log a warning. Phase-C (post-split-removal) the
# planner is supposed to thematically split; a fat FileEditPlan signals it
# ignored guidance and the edit will likely exhaust turn budget.
_HEAVY_EDIT_FUNC_WARN = 8
_HEAVY_EDIT_FINDING_WARN = 12


def _warn_if_heavy(edit: FileEditPlan) -> None:
    """Print a warning when a single FileEditPlan crosses heuristic
    heaviness thresholds. Does NOT modify the plan or block execution —
    the planner remains authoritative.
    """
    n_funcs = len(edit.target_functions)
    n_findings = len(edit.preserved_findings)
    if n_funcs >= _HEAVY_EDIT_FUNC_WARN or n_findings >= _HEAVY_EDIT_FINDING_WARN:
        print(
            f"[patch-generator] WARNING: heavy FileEditPlan for {edit.filepath} "
            f"(target_functions={n_funcs}, preserved_findings={n_findings}). "
            "Patch-planner should have split this thematically; expect long "
            "prompts and possible turn-budget exhaustion.",
            flush=True,
        )


def _build_single_edit_prompt(
    memory: SharedWorkingMemory,
    edit: FileEditPlan,
    *,
    sub_edit_label: str = "",
    retry_preamble: str = "",
) -> str:
    """Construct a focused prompt for one (sub-)edit plan."""
    findings = "\n".join(f"- {item}" for item in edit.preserved_findings) or "- (none)"
    targets = ", ".join(edit.target_functions) or "(unspecified)"
    co_edits = ", ".join(edit.co_edit_dependencies) or "(none)"
    req_section = _build_requirement_section(memory, edit)
    scope_note = (
        f"Scope: this run patches ONLY function(s) {targets} in {edit.filepath}. "
        "Do NOT touch other parts of the file.\n\n"
        if sub_edit_label
        else ""
    )
    return (
        f"{retry_preamble}"
        "Execute the following single-file patch plan. Only patch the target file in this run.\n\n"
        f"{scope_note}"
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
        "- Apply minimal SEARCH/REPLACE edits only to the listed target_functions.\n"
        "- Respect preserved_findings as hard constraints.\n"
        "- You MUST call mcp__patch__apply_search_replace at least once before finishing.\n"
        "- If this file cannot be patched, output PATCH_INCOMPLETE explicitly.\n"
        "- If the file is successfully patched, output PATCH_APPLIED.\n"
    )


def _count_apply_actions(memory: SharedWorkingMemory, filepath: str) -> int:
    """Count apply_search_replace events recorded for *filepath*."""
    return sum(
        1
        for event in memory.action_history
        if event.phase == "patch-generation"
        and event.subagent == "apply_search_replace"
        and event.outcome.endswith(f":{filepath}")
    )


async def _attempt_sub_edit(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
    *,
    sub_edit_label: str,
    attempt_idx: int,
    retry_preamble: str,
    failure_logger: PatchFailureLogger,
) -> AttemptOutcome:
    """Run one attempt at a single FileEditPlan.

    Phase-C: the unit of work is a FileEditPlan (the planner's atomic edit
    intent). Same filepath may legitimately appear in multiple
    FileEditPlans, each with its own theme. The label parameter
    ``sub_edit_label`` is a free-form display string used in logs only;
    it stays named ``sub_edit_label`` to keep call sites stable but the
    concept is now "edit attempt label".

    Returns an AttemptOutcome:
    - CHANGED: file hash changed after the attempt — real edit applied.
    - IDEMPOTENT: hash unchanged but model signaled "already at target state"
      via PATCH_APPLIED / "already correct" / similar — accept as success
      without retry. Avoids the issue-010 pattern where a no-op edit gets
      retried 3× because the model correctly declined to call MCP.
    - FAILED: hash unchanged with no idempotency signal — true silent failure.

    tool_calls_delta is logged for diagnostics but is no longer the success
    criterion: file content + result text are the truth.
    """
    prompt = _build_single_edit_prompt(
        memory,
        edit,
        sub_edit_label=sub_edit_label,
        retry_preamble=retry_preamble,
    )
    actions_before = _count_apply_actions(memory, edit.filepath)
    abs_target = repo_dir / edit.filepath
    hash_before = _file_hash(abs_target)

    limits_idx = min(attempt_idx, len(_ATTEMPT_LIMITS)) - 1
    max_turns, max_budget = _ATTEMPT_LIMITS[limits_idx]
    print(
        f"[patch-generator] {sub_edit_label} attempt {attempt_idx}: "
        f"prompt={len(prompt)} chars, max_turns={max_turns}, max_budget=${max_budget}",
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
        max_turns=max_turns,
        max_budget_usd=max_budget,
        permission_mode="acceptEdits",
    )

    result_text = ""
    limit_hit: str | None = None
    subtype = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                subtype = message.subtype or ""
                if subtype in ("error_max_turns", "error_max_budget_usd"):
                    limit_hit = subtype

    actions_after = _count_apply_actions(memory, edit.filepath)
    delta = actions_after - actions_before
    hash_after = _file_hash(abs_target)
    hash_changed = (
        hash_before is not None
        and hash_after is not None
        and hash_before != hash_after
    )

    classification = _classify_attempt(
        hash_changed=hash_changed,
        tool_calls_delta=delta,
        result_text=result_text,
    )

    result_preview = _safe_preview(result_text, limit=400)
    print(
        f"[patch-generator] {sub_edit_label} attempt {attempt_idx} done: "
        f"subtype={subtype} tool_calls_delta={delta} hash_changed={hash_changed} "
        f"classification={classification} limit_hit={limit_hit} "
        f"result='{result_preview}'",
        flush=True,
    )

    if classification == "FAILED":
        failure_logger.attempt_failed(
            filepath=edit.filepath,
            sub_label=sub_edit_label,
            attempt_idx=attempt_idx,
            prompt_chars=len(prompt),
            max_turns=max_turns,
            max_budget=max_budget,
            subtype=subtype,
            limit_hit=limit_hit,
            tool_calls_delta=delta,
            hash_changed=hash_changed,
            classification=classification,
            result_preview=result_preview,
        )

    return classification


def _retry_preamble_for(
    sub_edit_label: str,
    prior_attempt_signal: str,
) -> str:
    """Compose a directive preamble for retry attempts."""
    return (
        f"PRIOR ATTEMPT FOR {sub_edit_label} FAILED ({prior_attempt_signal}).\n"
        "Common cause: too many turns spent thinking before producing the first "
        "SEARCH/REPLACE block. To avoid this:\n"
        "1. Issue exactly ONE Read on the target file.\n"
        "2. Immediately produce SEARCH/REPLACE blocks and call "
        "mcp__patch__apply_search_replace.\n"
        "3. Skip TodoWrite and skip extended deliberation.\n"
        "4. Output PATCH_APPLIED on success or PATCH_INCOMPLETE on failure.\n\n"
    )


async def _run_single_edit(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
    edit_idx: int,
    edit_total: int,
    failure_logger: PatchFailureLogger,
    prior_changed_files: set[str] | None = None,
) -> bool:
    """Run patch generation for a single FileEditPlan (atomic unit).

    Phase-C contract: the patch-planner is responsible for thematic split.
    Each FileEditPlan is a focused, self-contained edit. The patch-generator
    no longer attempts to re-split heavy edits — it just retries with
    progressively higher turn/budget limits and reports failure cleanly.

    Pipeline:
    1. Optional warning if the FileEditPlan is heuristically "heavy"
       (planner ignored thematic-split guidance). Does not block.
    2. Up to _SUB_EDIT_MAX_ATTEMPTS attempts. CHANGED or IDEMPOTENT both
       count as success; FAILED triggers retry.
    3. Final acceptance: success classification AND the file appears in
       `git diff` for its planned path. A fully-IDEMPOTENT FileEditPlan
       with no diff signals the planner asked for a no-op edit; the
       coverage check catches that and downgrades to failure.
    """
    _warn_if_heavy(edit)

    # Same filepath may appear in multiple FileEditPlans (phase C). Label
    # disambiguates per-edit so logs are unambiguous; downstream tools
    # parse this label as opaque.
    primary_func = (
        edit.target_functions[0]
        if edit.target_functions
        else "(file-wide pass)"
    )
    edit_label = f"{edit.filepath} edit {edit_idx}/{edit_total} ({primary_func})"

    outcome: AttemptOutcome = "FAILED"
    prior_signal = "no apply_search_replace tool call"
    for attempt in range(1, _SUB_EDIT_MAX_ATTEMPTS + 1):
        preamble = (
            ""
            if attempt == 1
            else _retry_preamble_for(edit_label, prior_signal)
        )
        outcome = await _attempt_sub_edit(
            memory,
            repo_dir,
            edit,
            sub_edit_label=edit_label,
            attempt_idx=attempt,
            retry_preamble=preamble,
            failure_logger=failure_logger,
        )
        if outcome in ("CHANGED", "IDEMPOTENT"):
            break

    if outcome == "CHANGED":
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"EDIT_SUCCESS:{edit_label}",
        )
    elif outcome == "IDEMPOTENT":
        print(
            f"[patch-generator] {edit_label}: idempotent (file already at target state)",
            flush=True,
        )
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"EDIT_IDEMPOTENT:{edit_label}",
        )
    else:
        # If every attempt returned empty (no tool calls, no result text) AND
        # a prior edit in this run already CHANGED the same file, the most
        # likely explanation is that the file-wide rename/rewrite made all
        # subsequent per-function edits no-ops: the LLM reads the already-
        # patched file, sees nothing left to do, and returns silently instead
        # of saying "PATCH_APPLIED". Treat this as IDEMPOTENT rather than
        # failure so the overall patch is not aborted.  The coverage check
        # below (missing_from_diff) still applies: if the file really did not
        # change at all we will catch it there.
        filepath_norm = edit.filepath.replace("\\", "/")
        prior = prior_changed_files or set()
        if filepath_norm in prior:
            print(
                f"[patch-generator] {edit_label}: silent after prior CHANGED edit "
                f"for same file — promoting to IDEMPOTENT (prior edit likely covered it)",
                flush=True,
            )
            outcome = "IDEMPOTENT"
            memory.record_action(
                phase="patch-generation",
                subagent="patch-generator",
                outcome=f"EDIT_IDEMPOTENT_PROMOTED:{edit_label}",
            )
        else:
            print(
                f"[patch-generator] {edit_label}: gave up after {_SUB_EDIT_MAX_ATTEMPTS} attempts",
                flush=True,
            )
            memory.record_action(
                phase="patch-generation",
                subagent="patch-generator",
                outcome=f"EDIT_FAILED:{edit_label}",
            )

    diff_text = _run_git_diff(repo_dir, [edit.filepath])
    missing_from_diff = _planned_files_present_in_diff(diff_text, [edit.filepath])
    print(
        f"[patch-generator] Verification: {edit_label} "
        f"outcome={outcome} missing_from_diff={missing_from_diff}",
        flush=True,
    )

    edit_ok = outcome in ("CHANGED", "IDEMPOTENT") and not missing_from_diff
    final_status = "PATCH_SUCCESS" if edit_ok else "PATCH_FAILED"
    failure_logger.file_summary(
        filepath=edit.filepath,
        sub_edits_total=1,
        sub_edits_changed=1 if outcome == "CHANGED" else 0,
        sub_edits_idempotent=1 if outcome == "IDEMPOTENT" else 0,
        sub_edits_failed=1 if outcome == "FAILED" else 0,
        fallback_used=False,
        fallback_outcome=None,
        missing_from_diff=missing_from_diff,
        final_status=final_status,
    )

    return edit_ok


async def _run_patch_generator_async(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    output_dir: Path | None = None,
) -> bool:
    """Run the patch generator agent. Returns True iff every FileEditPlan applied.

    Phase-C model: each FileEditPlan is an atomic unit. Same filepath may
    appear multiple times in plan.edits — each instance is its own focused
    edit (e.g. "field rename pass" + "audit context fix" both targeting
    forwarder.go). The generator no longer splits or merges; it just runs
    each FileEditPlan with attempt-level retries.

    No early-abort: each edit is attempted independently regardless of prior
    failures.  This is required for SWE-bench Pro scoring, which only credits
    a case when the entire test suite passes — partial coverage is worthless,
    so an aborted failing edit should not silently kill remaining edits.

    When *output_dir* is provided, structured failure events are appended to
    ``<output_dir>/patch_failures.log`` (jsonl) for post-mortem analysis.
    """
    failure_logger = PatchFailureLogger(output_dir)

    sanitized_plan = _sanitize_patch_plan(memory, repo_dir)
    if sanitized_plan is None or not sanitized_plan.edits:
        print("[patch-generator] No valid planned edits remain after sanitization", flush=True)
        failure_logger.run_summary(files_total=0, files_succeeded=0, files_failed=0)
        return False

    edit_total = len(sanitized_plan.edits)
    print(
        f"[patch-generator] Running focused patch generation for {edit_total} edits",
        flush=True,
    )
    for edit in sanitized_plan.edits:
        print(
            f"  - {edit.filepath} ({len(edit.preserved_findings)} preserved_findings, "
            f"{len(edit.target_functions)} target_functions)",
            flush=True,
        )

    edits_succeeded = 0
    edits_failed = 0
    changed_files: set[str] = set()
    for edit_idx, edit in enumerate(sanitized_plan.edits, 1):
        ok = await _run_single_edit(
            memory, repo_dir, edit, edit_idx, edit_total, failure_logger,
            prior_changed_files=changed_files,
        )
        if ok:
            edits_succeeded += 1
            # Track files where an edit actually landed so subsequent edits
            # for the same file can detect "prior CHANGED" promotion.
            changed_files.add(edit.filepath.replace("\\", "/"))
            memory.record_action(
                phase="patch-generation",
                subagent="patch-generator",
                outcome=f"PATCH_SUCCESS:{edit.filepath}",
            )
        else:
            edits_failed += 1
            memory.record_action(
                phase="patch-generation",
                subagent="patch-generator",
                outcome=f"PATCH_FAILED:{edit.filepath}",
            )

    failure_logger.run_summary(
        files_total=edit_total,
        files_succeeded=edits_succeeded,
        files_failed=edits_failed,
    )
    return edits_failed == 0


def run_patch_generator(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    output_dir: Path | None = None,
) -> bool:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with patch plan and cached code.
        repo_dir: Repository root directory.
        output_dir: Optional directory where patch_failures.log is written.

    Returns:
        True if patches were successfully applied.
    """
    return asyncio.run(_run_patch_generator_async(memory, repo_dir, output_dir))
