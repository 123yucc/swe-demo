"""
Patch Planner sub-agent (phase 18.D): reads EvidenceCards and produces a
structured PatchPlan with preserved_findings for constraint propagation.
"""

import asyncio
import re
from pathlib import Path

from src.agents._structured import run_structured_query
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan

# Prescriptive patterns that indicate boundary constraints to preserve.
_PRESCRIPTIVE_PATTERNS = (
    re.compile(r"`[^`]+`"),  # backtick-enclosed code
    re.compile(r"correct (?:form|value|comparison) is?\s*[:\s]+"),
    re.compile(r"should be\s*[:\s]+"),
    re.compile(r"must be\s*[:\s]+"),
    re.compile(r"must use\s+"),
    re.compile(r"instead of\s+"),
    re.compile(r"change\s+\w+\s+to\s+"),
    re.compile(r"replace\s+\w+\s+with\s+"),
    re.compile(r"correct|should be|must be|正确|应改为"),
    re.compile(r"\(\s*\w+\s+\|\|\s+Date\.now\(\)\s*\)"),  # specific ttl formula
    re.compile(r"ttl\s*\|\|\s*Date\.now\(\)"),
)


# Language-agnostic path detection: one or more non-space path segments
# separated by "/", ending in ``.<ext>`` where ext is 1-6 alphanum chars.
# Works for .py, .js, .ts, .go, .rs, .java, .cpp, .rb, .php, etc.
_FILE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,6})\b")

# Verbs that, when co-occurring with a file path inside a single co-edit
# relation, signal the file is a required edit target (as opposed to a
# reference/mention).  Language- and framework-neutral.
_COEDIT_ACTION_VERBS = (
    "must be updated", "must be modified", "must be changed",
    "must be removed", "must be deleted",
    "must be added", "must be created", "must be created first",
    "must be registered", "must be mounted", "must be exported",
    "must be imported", "must be wired", "must be integrated",
    "must gain", "must contain", "must include",
    "must reference", "must invoke", "must import", "must export",
    "must point to",
    # cross-edit direction ("X -> Y must ...") is expressed as an arrow.
    "->",
)


def _is_new_file_edit(edit: FileEditPlan, memory: SharedWorkingMemory) -> bool:
    """Heuristic: a file is 'new' when its path is absent from retrieved_code.

    The deep-search agent caches every file it reads into memory.retrieved_code;
    a planned edit target that was never cached is almost certainly a file
    the plan intends to create.
    """
    cached_paths = {key.split(":", 1)[0] for key in memory.retrieved_code.keys()}
    return edit.filepath not in cached_paths


def _extract_coedit_targets(memory: SharedWorkingMemory) -> list[tuple[str, str]]:
    """Collect (filepath, source_sentence) pairs for co-edit targets.

    Only scans ``structural.must_co_edit_relations`` — by contract this field
    holds sentences whose mentioned files are edit targets.  We deliberately
    exclude ``dependency_propagation`` because its arrow notation ``A -> B``
    means "A depends on B at runtime" (call/import/data-flow), which includes
    many read-only dependencies that must NOT be auto-edited.

    Within must_co_edit_relations, a sentence qualifies as a co-edit
    declaration if it contains at least one action verb (see
    ``_COEDIT_ACTION_VERBS``).  Every file path mentioned in such a
    sentence is returned.
    """
    if memory.evidence_cards is None:
        return []

    results: list[tuple[str, str]] = []
    for sentence in memory.evidence_cards.structural.must_co_edit_relations:
        sentence_lower = sentence.lower()
        has_action = any(verb in sentence_lower for verb in _COEDIT_ACTION_VERBS)
        if not has_action:
            continue
        for path_match in _FILE_PATH_RE.finditer(sentence):
            path = path_match.group(1).strip()
            if "/" not in path:
                continue
            results.append((path, sentence))
    return results


def _extract_prescriptive_snippets(findings: str) -> list[str]:
    """Extract prescriptive snippets from findings that must be preserved."""
    snippets: list[str] = []
    # Extract backtick-enclosed tokens
    for m in re.finditer(r"`([^`]+)`", findings):
        snippet = m.group(1).strip()
        if len(snippet) >= 3 and snippet not in [s for s in snippets]:
            snippets.append(snippet)
    # Extract lines with prescriptive keywords
    for line in findings.split("\n"):
        lower = line.lower()
        if any(kw in lower for kw in ("correct", "should be", "must be", "正确", "应改为")):
            line = line.strip()
            if line and line not in snippets:
                snippets.append(line)
    return snippets


PATCH_PLANNER_SYSTEM_PROMPT = """\
You are a Senior Staff Engineer planning a precise bug fix.

Review evidence cards and cached code, then produce a strategic edit plan.
You may also use long-term memory progressively: search summary cards first,
then browse selected experience ids in detail if they seem analogous.
Focus on: exact_code_regions, call_chain_context, behavioral_constraints,
backward_compatibility, missing_elements_to_implement,
must_co_edit_relations, dependency_propagation.

CRITICAL — co-edit completeness (framework-agnostic):
Every file path that appears in `structural.must_co_edit_relations`
inside a sentence that uses a co-edit action verb ("must be updated",
"must be modified", "must be registered", "must be mounted", "must be
exported", "must be removed", "must be created", "->", etc.) MUST
appear as its own FileEditPlan.  These sentences describe HARD
dependencies: if the file is not edited, the change is incomplete and
the program will fail at runtime (undefined references, unmounted
routes, unexported symbols, orphaned imports).

NOTE: `structural.dependency_propagation` is a RUNTIME dependency graph
(A -> B means "A depends on B" via import/call/data-flow).  Its targets
are mostly READ-ONLY dependencies — do NOT promote them to edits unless
the same file is also named in must_co_edit_relations.

Rule of thumb: for each sentence in must_co_edit_relations, ask "does
this sentence say some file must be changed?"  If yes, that file goes
into edits.  Do this regardless of language, framework, or directory
convention.

CRITICAL — thematic split: one filepath may appear MULTIPLE TIMES in `edits`.
The unit `FileEditPlan` is "one focused change to a file", not "all
changes to a file".  When a single file needs unrelated changes, emit
multiple FileEditPlan entries that share the same `filepath`.

Heuristic for whether to split:
- Function-driven changes (1-3 related functions sharing a single
  rationale) → ONE FileEditPlan listing those functions in
  target_functions.
- Horizontal rule-sets across the whole file (e.g. "rename field X to Y
  in all references", "replace ctx with f.ctx in every audit-emit call")
  → ONE FileEditPlan with `target_functions` left empty (or with a
  short tag like "(file-wide rename pass)") and the rule listed
  explicitly in change_rationale and preserved_findings.
- Two unrelated themes in the same file → TWO FileEditPlan entries.

Why this matters: a single FileEditPlan that mixes 20 functions and 19
findings makes the downstream patch-generator load a 25k-char prompt for
every retry attempt, exhausting turn budget before producing any edit.
A focused FileEditPlan with 1-3 functions or a single horizontal rule
keeps prompts under 10k chars and lets the generator finish in one pass.

Counter-example to AVOID (issue 009 forwarder.go shape):
  ONE FileEditPlan with 21 target_functions and 19 preserved_findings
  mixing field renames + audit context fix + cert validation +
  router privatisation.

Correct shape (same fix, split):
  - FileEditPlan(filepath=forwarder.go, target_functions=[],
      change_rationale="Field rename pass: Auth->Authz, Client->AuthClient,
      AccessPoint->CachingAuthClient, Tunnel->ReverseTunnelSrv,
      PingPeriod->ConnPingPeriod across the whole file",
      preserved_findings=[<the 5 rename rules>])
  - FileEditPlan(filepath=forwarder.go,
      target_functions=[Forwarder.exec, Forwarder.portForward, Forwarder.catchAll],
      change_rationale="Audit context leak: replace req.Context() with f.ctx
      in audit-emit call sites",
      preserved_findings=[<the audit-context findings>])
  - FileEditPlan(filepath=forwarder.go,
      target_functions=[Forwarder.requestCertificate, Forwarder.setClusterSession],
      change_rationale="Cert NotAfter validation before caching",
      preserved_findings=[<the cert findings>])
  - ... etc

CRITICAL — preserved_findings (phase 18.D):
For each FileEditPlan, you MUST populate the `preserved_findings` field
with verbatim prescriptive snippets from RequirementItem.findings that
apply to this FileEditPlan's specific theme.  Copy these EXACTLY — do
not summarize or paraphrase.

Distribute findings by theme, do NOT broadcast all findings to every
FileEditPlan.  Every prescriptive finding must end up in at least one
FileEditPlan (a code-level coverage gate will detect orphans and
auto-attach them, but relying on this is poor planning).

Prescriptive snippets include:
- Backtick-enclosed code tokens (e.g. `db.mget`, `ttl || Date.now()`)
- Lines containing "correct is", "should be", "must be", "instead of"
- Specific formula or comparison expressions
- Rename rules of the form "X should be Y" / "X -> Y"

Example GOOD preserved_findings:
  ["`ttl || Date.now() + interval > max`", "correct comparison: (ttl || Date.now()) + interval > max"]

Example BAD (summarized, not preserved):
  ["use correct ttl formula"]   ← paraphrased, loses the formula

Rules:
- EVIDENCE-GROUNDED: every file justified by evidence
- COMPLETE: include every co-edit target declared in
  must_co_edit_relations / dependency_propagation with an action verb
- MINIMAL & SUFFICIENT: smallest change set that fully fixes the defect
- SPLIT BY THEME: same filepath may appear in multiple FileEditPlans
- ORDER: list edits in dependency order (dependencies first)
- NO CODE: describe *what* and *why*, not actual code
- TO-BE items in constraints describe behaviors to ADD, not existing ones
- preserved_findings: copy verbatim, never summarize, distribute by theme

Return a structured JSON object matching the required schema.
"""


def _backfill_declared_coedit_files(
    plan: PatchPlan, memory: SharedWorkingMemory, repo_dir: Path | None = None
) -> list[str]:
    """Ensure every file declared as a co-edit target in evidence is in the plan.

    Language- and framework-agnostic: scans evidence sentences in
    ``must_co_edit_relations`` and ``dependency_propagation``, extracts every
    file path that co-occurs with a co-edit action verb, and appends a
    FileEditPlan for any such path that is missing from ``plan.edits``.

    When *repo_dir* is provided, paths that do not resolve to an existing
    file in the repo are skipped with a warning.  This guards against the
    failure mode observed on issue 010: deep-search wrote co-edit sentences
    with truncated paths (``lastfm/client.go`` instead of
    ``core/agents/lastfm/client.go``) and the regex picked them up; the
    auto-added edits then failed at patch-generation time because the file
    didn't exist on disk.  Filtering at backfill time prevents that 14-edit
    waste.

    Returns the list of filepaths that were appended (for logging).  Mutates
    ``plan.edits`` in place.
    """
    existing_paths = {edit.filepath for edit in plan.edits}
    appended: list[str] = []
    skipped_missing: list[str] = []
    seen_new: set[str] = set()

    for path, sentence in _extract_coedit_targets(memory):
        if path in existing_paths or path in seen_new:
            continue
        if repo_dir is not None and not (repo_dir / path).is_file():
            # Path mentioned in evidence but absent from disk — almost
            # always a truncated/malformed path from deep-search prose
            # rather than a real co-edit target.  Skip rather than blindly
            # append (which would create a guaranteed PATCH_FAILED edit).
            skipped_missing.append(path)
            continue
        seen_new.add(path)
        plan.edits.append(
            FileEditPlan(
                filepath=path,
                target_functions=["(declared co-edit target; see rationale)"],
                change_rationale=(
                    f"Auto-added by planner backfill: this file is declared "
                    f"as a required co-edit target in evidence but was "
                    f"missing from the model's edit list. Declaring sentence: "
                    f"{sentence!r}. Without editing this file, one or more "
                    f"other planned edits will be unreachable, unexported, or "
                    f"produce undefined references at runtime."
                ),
                preserved_findings=[],
                co_edit_dependencies=[],
            )
        )
        appended.append(path)

    if skipped_missing:
        print(
            f"[patch-planner] backfill skipped {len(skipped_missing)} path(s) "
            f"absent from repo: {skipped_missing}",
            flush=True,
        )

    return appended


def _collect_all_prescriptive_findings(
    memory: SharedWorkingMemory,
) -> list[tuple[str, str]]:
    """Return ``(req_id, snippet)`` pairs for every prescriptive snippet
    found across all requirements' findings.

    Used by the coverage gate to detect findings the planner failed to
    distribute to any FileEditPlan.

    Skips ``AS_IS_COMPLIANT`` requirements: their findings describe code
    that already satisfies the spec (e.g. "no external caller references
    `lastfm.Client`"), not constraints to enforce in the patch. Treating
    them as prescriptive turned every backticked identifier in such a
    finding into an orphan, which the coverage gate then dumped wholesale
    into the FileEditPlan whose path happened to match — converting a
    cleanly-themed 9-edit plan into one 30-finding heavy plan.
    """
    if memory.evidence_cards is None:
        return []
    pairs: list[tuple[str, str]] = []
    seen_per_req: dict[str, set[str]] = {}
    for req in memory.evidence_cards.requirements:
        if req.verdict == "AS_IS_COMPLIANT":
            continue
        if not req.findings:
            continue
        seen = seen_per_req.setdefault(req.id, set())
        for snippet in _extract_prescriptive_snippets(req.findings):
            if snippet in seen:
                continue
            seen.add(snippet)
            pairs.append((req.id, snippet))
    return pairs


def _snippet_is_present(snippet: str, plan: PatchPlan) -> bool:
    """Return True if *snippet* appears as a substring of any FileEditPlan's
    preserved_findings.  Substring rather than equality because the planner
    may make minor whitespace/punctuation tweaks; a strict match would fire
    too many false-positive orphans.
    """
    needle = snippet.strip()
    if not needle:
        return True
    for edit in plan.edits:
        for kept in edit.preserved_findings:
            if needle in kept:
                return True
    return False


def _assign_orphan_finding(
    snippet: str,
    req_id: str,
    plan: PatchPlan,
    memory: SharedWorkingMemory,
) -> tuple[str, list[str]]:
    """Decide where an orphan prescriptive snippet should be attached.

    Priority:
    (a) the snippet text itself names a filepath that exists in plan.edits
    (b) the source RequirementItem (findings + evidence_locations) names a
        filepath that exists in plan.edits — pick the first one found
    (c) no path match — broadcast to every FileEditPlan (caller decides)

    Returns ``(strategy, target_filepaths)``.  ``strategy`` is one of
    ``"snippet_path"``, ``"req_path"``, ``"broadcast"``.
    """
    plan_paths = [edit.filepath for edit in plan.edits]
    plan_paths_norm = {p.replace("\\", "/"): p for p in plan_paths}

    # (a) snippet itself names a planned filepath
    for match in _FILE_PATH_RE.finditer(snippet):
        cand = match.group(1).replace("\\", "/")
        if cand in plan_paths_norm:
            return "snippet_path", [plan_paths_norm[cand]]

    # (b) source req names a planned filepath
    if memory.evidence_cards is not None:
        for req in memory.evidence_cards.requirements:
            if req.id != req_id:
                continue
            haystack = "\n".join([req.findings, *req.evidence_locations])
            for match in _FILE_PATH_RE.finditer(haystack):
                cand = match.group(1).replace("\\", "/")
                if cand in plan_paths_norm:
                    return "req_path", [plan_paths_norm[cand]]
            break

    # (c) broadcast
    return "broadcast", list(plan_paths)


def _enforce_preserved_findings_coverage(
    plan: PatchPlan, memory: SharedWorkingMemory
) -> None:
    """Code-level gate: every prescriptive finding from the evidence must end
    up in at least one FileEditPlan.preserved_findings.

    The planner LLM is encouraged to distribute findings thematically (see
    PATCH_PLANNER_SYSTEM_PROMPT) but is not perfectly reliable.  This gate
    detects orphans and auto-attaches them; without it, a missing
    prescriptive constraint would silently become invisible to the
    patch-generator.

    Mutates ``plan.edits[*].preserved_findings`` in place.  Logs every
    orphan attachment so batch runs can spot when the planner is
    chronically dropping findings.
    """
    orphans = [
        (req_id, snippet)
        for req_id, snippet in _collect_all_prescriptive_findings(memory)
        if not _snippet_is_present(snippet, plan)
    ]
    if not orphans:
        return

    print(
        f"[patch-planner] preserved_findings coverage gate: "
        f"{len(orphans)} orphan snippet(s) detected; auto-attaching",
        flush=True,
    )

    for req_id, snippet in orphans:
        strategy, targets = _assign_orphan_finding(snippet, req_id, plan, memory)
        target_set = set(targets)
        # When multiple FileEditPlan entries share a filepath (the planner
        # split the file by theme), attach the orphan to ONLY the first
        # matching plan. Iterating across all of them dumps the same snippet
        # 8x into 8 distinct plans, ballooning every plan's prompt past the
        # patch-generator turn budget. Once is enough — the snippet is now
        # in the working set, satisfying the coverage gate's existence
        # invariant. Themed re-attachment is the planner's job, not ours.
        attached_paths: set[str] = set()
        for edit in plan.edits:
            if edit.filepath not in target_set:
                continue
            if edit.filepath in attached_paths:
                continue
            if snippet not in edit.preserved_findings:
                edit.preserved_findings.append(snippet)
            attached_paths.add(edit.filepath)
        snippet_preview = snippet if len(snippet) <= 80 else snippet[:77] + "..."
        print(
            f"[patch-planner]   orphan: req={req_id} strategy={strategy} "
            f"-> {targets}: {snippet_preview!r}",
            flush=True,
        )


def _deduplicate_shared_findings(plan: PatchPlan) -> None:
    """For each group of FileEditPlan entries that share the same filepath,
    remove findings that are duplicated across the group.

    When the planner splits one file into multiple themed FileEditPlans it
    should distribute preserved_findings by theme. In practice the LLM tends
    to broadcast every finding to every plan for the same file ("to be safe"),
    which makes each plan's prompt 3-5× larger than necessary and causes the
    patch-generator to time out or return empty responses on large files.

    This post-processing pass keeps each finding in the FIRST plan of the
    group (preserving at least one copy, satisfying the coverage invariant)
    and removes it from all subsequent plans for the same filepath.

    Mutates ``plan.edits`` in place. Runs after the LLM returns and before
    the preserved-findings coverage gate (so the gate doesn't re-add them).
    """
    from collections import defaultdict
    filepath_to_edits: dict[str, list[FileEditPlan]] = defaultdict(list)
    for edit in plan.edits:
        filepath_to_edits[edit.filepath].append(edit)

    for filepath, edits in filepath_to_edits.items():
        if len(edits) < 2:
            continue
        # Find findings present in every plan (the broadcast set)
        all_sets = [set(e.preserved_findings) for e in edits]
        broadcast = set.intersection(*all_sets)
        if not broadcast:
            continue
        removed = 0
        for edit in edits[1:]:  # keep in first plan, remove from rest
            before = len(edit.preserved_findings)
            edit.preserved_findings = [f for f in edit.preserved_findings if f not in broadcast]
            removed += before - len(edit.preserved_findings)
        print(
            f"[patch-planner] dedup {filepath}: removed {removed} broadcast "
            f"finding(s) from {len(edits)-1} secondary plan(s) "
            f"({len(broadcast)} unique broadcast snippets)",
            flush=True,
        )


async def _run_patch_planner_async(
    memory: SharedWorkingMemory,
    repo_dir: Path | None = None,
) -> PatchPlan:
    prompt = (
        "Plan a bug fix based on the following context:\n\n"
        f"{memory.format_for_prompt()}\n\n"
        "Return a structured patch plan with preserved_findings per file."
    )

    plan = await run_structured_query(
        system_prompt=PATCH_PLANNER_SYSTEM_PROMPT,
        user_prompt=prompt,
        response_model=PatchPlan,
        component="patch-planner",
        allowed_tools=[],
        max_turns=20,
        max_budget_usd=1.5,
    )

    # ── Framework-agnostic co-edit backfill ──
    # Any file path declared in must_co_edit_relations / dependency_propagation
    # with an action verb that is absent from plan.edits is auto-added.
    # When repo_dir is provided, paths absent from disk are skipped — see
    # _backfill_declared_coedit_files for the rationale (issue 010).
    appended = _backfill_declared_coedit_files(plan, memory, repo_dir)
    if appended:
        print(
            f"[patch-planner] backfilled declared co-edit files: {appended}",
            flush=True,
        )

    # ── Dedup broadcast findings across plans for the same filepath ──
    # The planner LLM tends to copy all findings to every themed plan for
    # the same file. Remove the shared (broadcast) ones from secondary plans
    # so each plan stays focused and patch-generator prompts stay small.
    _deduplicate_shared_findings(plan)

    # ── Coverage gate: every prescriptive finding must reach at least one
    # FileEditPlan.  Replaces the older "fill empty preserved_findings with
    # everything" backfill — that was a broadcast that only fired when the
    # whole list was empty, leaving partial coverage silently broken.
    _enforce_preserved_findings_coverage(plan, memory)

    memory.patch_plan = plan
    return plan


def run_patch_planner(
    memory: SharedWorkingMemory,
    repo_dir: Path | None = None,
) -> PatchPlan:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with evidence cards and cached code.
        repo_dir: Optional repo root. When provided, backfill skips
            FileEditPlan auto-additions whose paths don't exist on disk
            (guards against truncated paths in deep-search prose).

    Returns:
        PatchPlan with per-file edit intents.
    """
    return asyncio.run(_run_patch_planner_async(memory, repo_dir))
