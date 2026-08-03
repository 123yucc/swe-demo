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
import os
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
from src.agents import _cost_tracker
from src.agents._backend import use_openai_backend
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan
from src.tools.patch_tools import apply_search_replace, create_file


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


class PatchGeneratorInfraError(RuntimeError):
    """Model/relay infrastructure failure that must not be scored as patch quality."""


def _is_infra_failure_signal(text: str) -> bool:
    lowered = (text or "").lower()
    infra_markers = (
        "buzz_error",
        "get_channel_failed",
        "insufficient balance",
        "error code: 403",
        "error code: 500",
        "certificate",
        "ssl",
        "tls",
        "x509",
        "connection refused",
        "connection reset",
        "remote protocol error",
    )
    return any(marker in lowered for marker in infra_markers)


def _is_explicit_patch_format_failure(signal: str) -> bool:
    lowered = (signal or "").lower()
    return any(
        marker in lowered
        for marker in (
            "malformed search/replace block",
            "did not contain search/replace blocks",
            "missing '======split' separator",
            "missing '>>>>>>replace' terminator",
            "search block is empty",
            "no search/replace blocks found",
        )
    )


def _creates_new_file(edit: FileEditPlan) -> bool:
    """Compatibility wrapper for older deserialized FileEditPlan objects."""
    return bool(getattr(edit, "creates_new_file", False))


def _writes_full_file(edit: FileEditPlan, repo_dir: Path | None = None) -> bool:
    """Return true when the edit should write complete file content."""
    if repo_dir is None:
        return _creates_new_file(edit)
    target = repo_dir / edit.filepath
    try:
        if not target.exists():
            return True
        if target.is_file() and target.stat().st_size == 0:
            return True
        # A planned new-file edit may be revisited during artifact/static
        # repair after the file has already been created. At that point a
        # second create_file call is a whole-file rewrite and tends to destroy
        # previously-correct symbols/imports. Repair existing non-empty files
        # with minimal SEARCH/REPLACE instead.
        return False
    except OSError:
        return _creates_new_file(edit)


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
    if lowered.lstrip().startswith("error:"):
        return "FAILED"
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
1. If the FileEditPlan creates a new file, do not Read the missing file; write
   the complete new file with mcp__patch__create_file
2. Otherwise, READ the target file before generating any SEARCH blocks
3. IDENTIFY exact code regions that need to change
4. CONSTRUCT SEARCH/REPLACE blocks:
   <<<<<<SEARCH
   [exact old code to find]
   ======SPLIT
   [new code to replace it with]
   >>>>>>REPLACE
5. CALL mcp__patch__apply_search_replace for each existing-file edit
6. If ERROR: re-read the file, adjust, and retry

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
- expected_symbols are hard constraints: if the FileEditPlan names expected
  symbols, define those exact spellings in that exact file before finishing.
  Do not rename them, case-flip them, abbreviate them, or move them to a
  sibling file. If the plan expects `requestLoggerContext`, defining
  `addLoggerToContext` is a failure; if it expects `getClientUniqueId`,
  creating `clientUuid` is a failure.
- NEVER edit test files. The evaluator owns the test suite and applies its own
  test patch on top of yours; any edit to a test file (paths under tests/ or
  test/, files named *_test.go, test_*.py, *_test.py, *.test.js, *.spec.ts,
  *.spec.js, or under __tests__/) is reverted before verification and can only
  collide with the evaluator's gold tests. If a plan entry points at a test
  file, skip it and edit only the production code.

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
    consolidated: list[FileEditPlan] = []
    by_scope: dict[tuple[str, tuple[str, ...]], int] = {}
    merged_count = 0
    for edit in kept:
        scope = (
            edit.filepath.replace("\\", "/").strip(),
            tuple(edit.target_functions),
        )
        # Empty target scope can represent independent file-wide themes; only
        # collapse explicit duplicate function/symbol scopes.
        if not edit.target_functions or scope not in by_scope:
            by_scope[scope] = len(consolidated)
            consolidated.append(edit.model_copy(deep=True))
            continue
        current = consolidated[by_scope[scope]]

        def merged(left: list[str], right: list[str]) -> list[str]:
            return [*left, *(item for item in right if item not in left)]

        rationales = [current.change_rationale]
        if edit.change_rationale not in rationales:
            rationales.append(edit.change_rationale)
        consolidated[by_scope[scope]] = current.model_copy(update={
            "change_rationale": "\nAdditional intent:\n".join(rationales),
            "preserved_findings": merged(
                current.preserved_findings, edit.preserved_findings
            ),
            "co_edit_dependencies": merged(
                current.co_edit_dependencies, edit.co_edit_dependencies
            ),
            "reference_only": current.reference_only and edit.reference_only,
            "expected_diff_required": (
                current.expected_diff_required or edit.expected_diff_required
            ),
            "creates_new_file": current.creates_new_file or edit.creates_new_file,
            "expected_symbols": merged(
                current.expected_symbols, edit.expected_symbols
            ),
            "required_by_requirement_ids": merged(
                current.required_by_requirement_ids,
                edit.required_by_requirement_ids,
            ),
        })
        merged_count += 1
    if merged_count:
        print(
            f"[patch-generator] Consolidated {merged_count} duplicate "
            "same-function planned edit(s).",
            flush=True,
        )
    sanitized = PatchPlan(overview=patch_plan.overview, edits=consolidated)
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


def _build_repair_context_section(memory: SharedWorkingMemory) -> str:
    """Return concise non-evidence repair context for focused edit prompts.

    The patch generator executes each FileEditPlan in isolation.  On direct
    compile/static repair rounds, the actionable signal lives in
    ``build_error_feedback`` and matched custom rules rather than in the old
    per-file preserved findings.  Keep this section compact but present in
    every focused prompt so single-file generation cannot ignore the reason
    the prior patch was rejected.
    """
    sections: list[str] = []
    if memory.custom_repair_block:
        sections.append(
            "## Custom Repair Discipline\n"
            f"{memory.custom_repair_block.strip()}"
        )
    if memory.build_error_feedback:
        sections.append(
            "## Blocking Verification Feedback\n"
            "The previous patch was rejected by Stage2 verification. Fix these "
            "items in the current target file without reverting the intended "
            "requirement-level behavior:\n"
            f"{memory.build_error_feedback.strip()}"
        )
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections) + "\n\n"


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
    repo_dir: Path,
    edit: FileEditPlan,
    *,
    sub_edit_label: str = "",
    retry_preamble: str = "",
) -> str:
    """Construct a focused prompt for one (sub-)edit plan."""
    findings = "\n".join(f"- {item}" for item in edit.preserved_findings) or "- (none)"
    targets = ", ".join(edit.target_functions) or "(unspecified)"
    co_edits = ", ".join(edit.co_edit_dependencies) or "(none)"
    expected_symbols = ", ".join(edit.expected_symbols) or "(none)"
    req_section = _build_requirement_section(memory, edit)
    repair_context = _build_repair_context_section(memory)
    if _writes_full_file(edit, repo_dir):
        target_exists = (repo_dir / edit.filepath).exists()
        scope_note = (
            f"Scope: this run creates ONLY {edit.filepath}. "
            "Do NOT edit other files in this turn.\n\n"
        )
        edit_instructions = (
            f"- target_exists={target_exists}; this filepath is treated as a planned full-file write.\n"
            "- Do NOT call Read on the missing target file.\n"
            "- Build the complete file content from the plan, preserved_findings, "
            "and nearby co-edit context if needed.\n"
            "- You MUST call mcp__patch__create_file with the complete file content before finishing.\n"
            "- If the file is successfully created, output PATCH_APPLIED.\n"
        )
    else:
        scope_note = (
            f"Scope: this run patches ONLY function(s) {targets} in {edit.filepath}. "
            "Do NOT touch other parts of the file.\n\n"
            if sub_edit_label
            else ""
        )
        edit_instructions = (
            "- Read the target file first.\n"
            "- Apply minimal SEARCH/REPLACE edits only to the listed target_functions.\n"
            "- You MUST call mcp__patch__apply_search_replace at least once before finishing.\n"
            "- If the file is successfully patched, output PATCH_APPLIED.\n"
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
        f"expected_symbols: {expected_symbols}\n"
        "preserved_findings:\n"
        f"{findings}\n"
        f"{req_section}\n\n"
        f"{repair_context}"
        "Instructions:\n"
        "- Respect preserved_findings as hard constraints.\n"
        "- Respect expected_symbols as hard constraints. If any are listed, "
        "the final file must define those exact symbol spellings in this file; "
        "do not substitute near-miss names or move them elsewhere.\n"
        f"{edit_instructions}"
        "- If this file cannot be patched, output PATCH_INCOMPLETE explicitly.\n"
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


def _compact_preserved_findings(findings: list[str], limit: int = 12) -> str:
    compact: list[str] = []
    seen: set[str] = set()
    for item in findings:
        text = " ".join(str(item).split())
        if not text or text in seen:
            continue
        seen.add(text)
        if len(text) > 900:
            text = text[:897].rstrip() + "..."
        compact.append(f"- {text}")
        if len(compact) >= limit:
            break
    return "\n".join(compact) or "- (none)"


def _target_search_tokens(targets: list[str]) -> list[str]:
    tokens: list[str] = []
    for target in targets:
        for raw in (
            (target or "").strip(),
            (target or "").strip().rsplit(".", 1)[-1],
            (target or "").strip().rsplit("::", 1)[-1],
        ):
            token = raw.split("(", 1)[0].strip()
            if token and token not in tokens and token != "(file-wide pass)":
                tokens.append(token)
    return tokens


def _build_current_file_prompt_context(
    current_content: str,
    targets: list[str],
) -> tuple[str, str]:
    """Return (label, body) for the file context section in direct prompts."""
    if not current_content or not targets:
        return "Current file content", f"```text\n{current_content}\n```"

    lines = current_content.splitlines()
    if len(lines) <= 260:
        return "Current file content", f"```text\n{current_content}\n```"

    token_hits: list[tuple[int, int]] = []
    for token in _target_search_tokens(targets):
        for idx, line in enumerate(lines):
            if token in line:
                start = max(0, idx - 40)
                end = min(len(lines), idx + 120)
                token_hits.append((start, end))
                break

    if not token_hits:
        return "Current file content", f"```text\n{current_content}\n```"

    ranges = [(0, min(len(lines), 120)), *token_hits]
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + 10:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    sections: list[str] = []
    excerpt_chars = 0
    for idx, (start, end) in enumerate(merged, 1):
        excerpt = "\n".join(lines[start:end]).rstrip()
        if not excerpt:
            continue
        excerpt_chars += len(excerpt)
        sections.append(
            f"Excerpt {idx} (lines {start + 1}-{end}):\n```text\n{excerpt}\n```"
        )
        if excerpt_chars >= 18000:
            break

    excerpt_body = "\n\n".join(sections).strip()
    if not excerpt_body or excerpt_chars >= int(len(current_content) * 0.85):
        return "Current file content", f"```text\n{current_content}\n```"
    return (
        "Current file excerpts",
        (
            "The following are verbatim excerpts from the target file. "
            "SEARCH text must be copied exactly from one excerpt.\n\n"
            + excerpt_body
        ),
    )


def _should_retry_file_wide(edit: FileEditPlan, prior_attempt_signal: str) -> bool:
    """Escalate a focused retry when the failure suggests top-level wiring drift.

    A target_functions-scoped edit can still need sibling import / constant /
    helper adjustments in the same file. When the prior attempt failed before
    applying any block because the SEARCH text did not match or the response
    never yielded a valid SEARCH/REPLACE payload, retry the SAME file as a
    file-wide pass rather than forcing a function-only scope that already
    failed.
    """
    if not edit.target_functions or edit.creates_new_file:
        return False
    signal = (prior_attempt_signal or "").lower()
    return any(
        marker in signal
        for marker in (
            "search text not found",
            "search text found",
            "malformed search/replace block",
            "did not contain search/replace blocks",
        )
    )


def _should_retry_missing_required_diff(
    edit: FileEditPlan,
    missing_from_diff: list[str],
) -> bool:
    """A required edit that vanished from git diff should get another try."""
    return bool(
        missing_from_diff
        and not edit.reference_only
        and getattr(edit, "expected_diff_required", True)
    )


def _can_accept_idempotent_noop(
    edit: FileEditPlan,
    missing_from_diff: list[str],
) -> bool:
    """Only optional/read-only edits may satisfy coverage without a diff."""
    return bool(
        missing_from_diff
        and (
            edit.reference_only
            or not getattr(edit, "expected_diff_required", True)
        )
    )


def _should_promote_silent_same_file_failure(signal: str) -> bool:
    normalized = (signal or "").strip().lower()
    return normalized in {"", "empty result", "no apply_search_replace tool call"}


def _build_openai_direct_patch_prompt(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
    *,
    sub_edit_label: str,
    retry_preamble: str = "",
    prior_error: str = "",
) -> str:
    targets = ", ".join(edit.target_functions) or "(file-wide pass)"
    co_edits = ", ".join(edit.co_edit_dependencies) or "(none)"
    findings = _compact_preserved_findings(edit.preserved_findings)
    req_section = _build_requirement_section(memory, edit)
    repair_context = _build_repair_context_section(memory)
    retry_section = (
        f"\n\nPrevious apply_search_replace error:\n{prior_error}\n"
        if prior_error
        else ""
    )

    if _writes_full_file(edit, repo_dir):
        target_exists = (repo_dir / edit.filepath).exists()
        return (
            f"{retry_preamble}"
            "Generate the complete content for a planned new or empty file. "
            "Do not answer PATCH_INCOMPLETE unless the requested file content "
            "is impossible from the supplied evidence.\n\n"
            f"Edit label: {sub_edit_label}\n"
            f"filepath: {edit.filepath}\n"
            f"target_exists: {target_exists}\n"
            f"target_symbols: {targets}\n"
            f"change_rationale: {edit.change_rationale}\n"
            f"co_edit_dependencies: {co_edits}\n\n"
            "Preserved findings / hard constraints:\n"
            f"{findings}\n"
            f"{req_section}"
            f"{repair_context}"
            f"{retry_section}\n\n"
            "Output rules:\n"
            "- Return only the full file content.\n"
            "- If you use a Markdown code fence, put only the file content "
            "inside the fence and no prose outside it.\n"
            "- Do not edit tests.\n"
        )

    target = repo_dir / edit.filepath
    try:
        current_content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        current_content = f"<<ERROR reading file: {exc}>>"
    context_label, context_body = _build_current_file_prompt_context(
        current_content,
        edit.target_functions,
    )

    return (
        f"{retry_preamble}"
        "Generate exact SEARCH/REPLACE blocks for the target file. "
        "Do not answer PATCH_INCOMPLETE unless the requested change is "
        "impossible from the supplied file content.\n\n"
        f"Edit label: {sub_edit_label}\n"
        f"filepath: {edit.filepath}\n"
        f"target_functions: {targets}\n"
        f"change_rationale: {edit.change_rationale}\n"
        f"co_edit_dependencies: {co_edits}\n\n"
        "Preserved findings / hard constraints:\n"
        f"{findings}\n"
        f"{req_section}"
        f"{repair_context}"
        f"{retry_section}\n\n"
        "Output rules:\n"
        "- Return only SEARCH/REPLACE blocks. Do not wrap them in JSON, "
        "Markdown, prose, or explanations.\n"
        "- `blocks` must use exactly this format, repeated as needed:\n"
        "<<<<<<SEARCH\n"
        "exact old text from the file\n"
        "======SPLIT\n"
        "replacement text\n"
        ">>>>>>REPLACE\n"
        "- SEARCH text must be copied verbatim from Current file content and "
        "must be unique in the file.\n"
        "- Do not edit tests.\n\n"
        f"{context_label}:\n"
        f"{context_body}"
    )


def _extract_search_replace_blocks(text: str) -> str:
    """Extract raw SEARCH/REPLACE blocks from an OpenAI free-text response."""
    if not text:
        return ""
    stripped = text.strip()
    if "<<<<<<SEARCH" not in stripped:
        return ""

    # If the model wrapped the answer in a code fence, the delimiter slicing
    # below still works.  It also tolerates brief accidental prose before/after.
    start = stripped.find("<<<<<<SEARCH")
    end = stripped.rfind(">>>>>>REPLACE")
    if end == -1:
        return ""
    end += len(">>>>>>REPLACE")
    return stripped[start:end].strip()


def _extract_full_file_content(text: str) -> str:
    """Extract full file content from a direct model response."""
    if not text:
        return ""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).rstrip() + "\n"
    return stripped.rstrip() + "\n"


async def _attempt_openai_direct_edit(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
    *,
    sub_edit_label: str,
    retry_preamble: str,
    max_turns: int,
) -> tuple[str, str]:
    if os.environ.get("OPENAI_AGENT_LOOP", "native").strip().lower() == "agents_sdk":
        from src.agents._openai_agents_sdk import run_agents_tool_agent as run_openai_tool_agent
    else:
        from src.agents._openai_native import run_openai_tool_agent

    prior_error = ""
    last_result = ""
    for _ in range(2):
        prompt = _build_openai_direct_patch_prompt(
            memory,
            repo_dir,
            edit,
            sub_edit_label=sub_edit_label,
            retry_preamble=retry_preamble,
            prior_error=prior_error,
        )
        try:
            system_prompt = (
                "You are a code patch generator. Return only the complete "
                "new file content requested by the prompt."
                if _writes_full_file(edit, repo_dir)
                else (
                    "You are a code patch generator. Return only exact "
                    "SEARCH/REPLACE blocks that can be applied mechanically."
                )
            )
            result = await run_openai_tool_agent(
                system_prompt=system_prompt,
                user_prompt=prompt,
                allowed_tools=[],
                max_turns=max_turns,
                cwd=str(repo_dir),
            )
        except Exception as exc:
            last_result = f"PATCH_INCOMPLETE: block generation failed: {exc}"
            prior_error = last_result
            continue

        if _writes_full_file(edit, repo_dir):
            content = _extract_full_file_content(result.result_text)
            if not content.strip():
                last_result = (
                    "PATCH_INCOMPLETE: OpenAI response did not contain new "
                    "file content. Response preview: "
                    + _safe_preview(result.result_text, limit=600)
                )
                prior_error = last_result
                continue
            result = await create_file.handler(
                {
                    "filepath": edit.filepath,
                    "content": content,
                    "overwrite": (repo_dir / edit.filepath).exists(),
                }
            )
            result_text = ""
            if isinstance(result, dict):
                result_text = " ".join(
                    str(item.get("text", ""))
                    for item in result.get("content", [])
                    if isinstance(item, dict)
                )
            last_result = result_text or str(result)
            if "Successfully created" in last_result:
                return "success", "PATCH_APPLIED: " + last_result
            prior_error = last_result
            continue

        blocks = _extract_search_replace_blocks(result.result_text)
        if not blocks:
            last_result = (
                "PATCH_INCOMPLETE: OpenAI response did not contain "
                "SEARCH/REPLACE blocks. Response preview: "
                + _safe_preview(result.result_text, limit=600)
            )
            prior_error = last_result
            continue

        result = await apply_search_replace.handler(
            {"filepath": edit.filepath, "blocks": blocks}
        )
        result_text = ""
        if isinstance(result, dict):
            result_text = " ".join(
                str(item.get("text", ""))
                for item in result.get("content", [])
                if isinstance(item, dict)
            )
        last_result = result_text or str(result)
        if "Successfully applied" in last_result:
            return "success", "PATCH_APPLIED: " + last_result
        prior_error = last_result

    return "success", last_result or "PATCH_INCOMPLETE"


async def _attempt_sub_edit(
    memory: SharedWorkingMemory,
    repo_dir: Path,
    edit: FileEditPlan,
    *,
    sub_edit_label: str,
    attempt_idx: int,
    retry_preamble: str,
    failure_logger: PatchFailureLogger,
) -> tuple[AttemptOutcome, str]:
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
        repo_dir,
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
        tools=[apply_search_replace, create_file],
    )

    options = ClaudeAgentOptions(
        system_prompt=PATCH_GENERATOR_SYSTEM_PROMPT,
        allowed_tools=[
            "Read",
            "mcp__patch__apply_search_replace",
            "mcp__patch__create_file",
            "TodoWrite",
        ],
        mcp_servers={"patch": patch_mcp},
        cwd=str(repo_dir),
        max_turns=max_turns,
        max_budget_usd=max_budget,
        permission_mode="acceptEdits",
    )

    result_text = ""
    limit_hit: str | None = None
    subtype = ""
    if use_openai_backend():
        subtype, result_text = await _attempt_openai_direct_edit(
            memory,
            repo_dir,
            edit,
            sub_edit_label=sub_edit_label,
            retry_preamble=retry_preamble,
            max_turns=max_turns,
        )
        if subtype == "error_max_turns":
            limit_hit = subtype
    else:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    _cost_tracker.accumulate(message)
                    result_text = message.result or ""
                    subtype = message.subtype or ""
                    if subtype in ("error_max_turns", "error_max_budget_usd"):
                        limit_hit = subtype

    actions_after = _count_apply_actions(memory, edit.filepath)
    delta = actions_after - actions_before
    hash_after = _file_hash(abs_target)
    hash_changed = hash_after is not None and hash_before != hash_after

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

    return classification, (result_preview or subtype or "empty result")


def _retry_preamble_for(
    sub_edit_label: str,
    prior_attempt_signal: str,
    *,
    file_wide_retry: bool = False,
) -> str:
    """Compose a directive preamble for retry attempts."""
    preamble = (
        f"PRIOR ATTEMPT FOR {sub_edit_label} FAILED ({prior_attempt_signal}).\n"
        "Common cause: too many turns spent thinking before producing the first "
        "SEARCH/REPLACE block. To avoid this:\n"
        "1. Issue exactly ONE Read on the target file.\n"
        "2. Immediately produce SEARCH/REPLACE blocks and call "
        "mcp__patch__apply_search_replace.\n"
        "3. Skip TodoWrite and skip extended deliberation.\n"
        "4. For planned new files, call mcp__patch__create_file instead of "
        "Read/apply_search_replace.\n"
        "5. Output PATCH_APPLIED on success or PATCH_INCOMPLETE on failure.\n\n"
    )
    if file_wide_retry:
        preamble += (
            "This retry is escalated to a SAME-FILE file-wide pass because the "
            "prior attempt likely needed top-level imports/helpers/constant "
            "wiring outside the named function. You may patch any necessary "
            "top-level code in this one file, but do NOT touch other files.\n\n"
        )
    return preamble


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
    3. Final acceptance: CHANGED edits must appear in `git diff`.
       IDEMPOTENT means the generator inspected the current file and affirmed
       that the planned target state is already present; it is persisted as a
       satisfied no-op (`reference_only`) so later artifact coverage does not
       mistake it for a silently dropped edit.
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
    infra_failure_signals: list[str] = []
    for attempt in range(1, _SUB_EDIT_MAX_ATTEMPTS + 1):
        escalated_retry = attempt > 1 and _should_retry_file_wide(edit, prior_signal)
        attempt_edit = (
            edit.model_copy(update={"target_functions": []})
            if escalated_retry
            else edit
        )
        preamble = (
            ""
            if attempt == 1
            else _retry_preamble_for(
                edit_label,
                prior_signal,
                file_wide_retry=escalated_retry,
            )
        )
        outcome, prior_signal = await _attempt_sub_edit(
            memory,
            repo_dir,
            attempt_edit,
            sub_edit_label=edit_label,
            attempt_idx=attempt,
            retry_preamble=preamble,
            failure_logger=failure_logger,
        )
        if outcome in ("CHANGED", "IDEMPOTENT"):
            attempt_diff = _run_git_diff(repo_dir, [edit.filepath])
            attempt_missing = _planned_files_present_in_diff(
                attempt_diff, [edit.filepath]
            )
            if (
                attempt < _SUB_EDIT_MAX_ATTEMPTS
                and _should_retry_missing_required_diff(edit, attempt_missing)
            ):
                prior_signal = (
                    f"{outcome} but {edit.filepath} is still absent from git "
                    "diff. Keep a real diff in this required file; do not "
                    "revert it to base, drop the planned change, or claim "
                    "the file is already correct unless the diff really exists."
                )
                continue
            break
        if _is_infra_failure_signal(prior_signal):
            infra_failure_signals.append(prior_signal)

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
        if (
            filepath_norm in prior
            and _should_promote_silent_same_file_failure(prior_signal)
            and not _is_explicit_patch_format_failure(prior_signal)
        ):
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
            if infra_failure_signals and not edit.reference_only:
                raise PatchGeneratorInfraError(
                    f"{edit.filepath}: model/relay infrastructure failure: "
                    f"{infra_failure_signals[-1]}"
                )
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

    # reference_only edits were auto-added by planner backfill from a co-edit
    # relation, not chosen as definite change targets.  Evidence often names
    # such files only as read-for-pattern context (e.g. "read user.js to learn
    # the privilege-check pattern").  When the generator correctly concludes no
    # change is needed (no diff), that is a legitimate NO_OP_OK — not a failure.
    # Forcing it to FAILED here is exactly what sank issue 003: a complete,
    # correct 6-file patch was reported PATCH_FAILED because one backfilled
    # reference file produced no diff.  An actual error (CHANGED but somehow
    # missing from diff, which cannot happen for a no-op) is still caught.
    if edit.reference_only and outcome == "FAILED" and missing_from_diff:
        print(
            f"[patch-generator] {edit_label}: reference_only edit produced no "
            f"diff — treating as NO_OP_OK (file was a read-for-pattern co-edit "
            f"target, not a required change).",
            flush=True,
        )
        outcome = "NO_OP_OK"
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"EDIT_NO_OP_OK:{edit_label}",
        )
        missing_from_diff = False

    if outcome == "IDEMPOTENT" and _can_accept_idempotent_noop(edit, missing_from_diff):
        print(
            f"[patch-generator] {edit_label}: verified target state already "
            "present; recording satisfied no-op for artifact coverage.",
            flush=True,
        )
        edit.reference_only = True
        memory.record_action(
            phase="patch-generation",
            subagent="patch-generator",
            outcome=f"EDIT_SATISFIED_NO_OP:{edit_label}",
        )
        missing_from_diff = False

    edit_ok = outcome in ("CHANGED", "IDEMPOTENT", "NO_OP_OK") and not missing_from_diff
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
