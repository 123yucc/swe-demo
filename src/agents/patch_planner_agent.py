"""
Patch Planner sub-agent (phase 18.D): reads EvidenceCards and produces a
structured PatchPlan with preserved_findings for constraint propagation.
"""

import asyncio
import re

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

CRITICAL — preserved_findings (phase 18.D):
For each FileEditPlan, you MUST populate the `preserved_findings` field
with verbatim prescriptive snippets from RequirementItem.findings that
apply to this file.  Copy these EXACTLY — do not summarize or paraphrase.

Prescriptive snippets include:
- Backtick-enclosed code tokens (e.g. `db.mget`, `ttl || Date.now()`)
- Lines containing "correct is", "should be", "must be", "instead of"
- Specific formula or comparison expressions

Example GOOD preserved_findings:
  ["`ttl || Date.now() + interval > max`", "correct comparison: (ttl || Date.now()) + interval > max"]

Example BAD (summarized, not preserved):
  ["use correct ttl formula"]   ← paraphrased, loses the formula

Rules:
- EVIDENCE-GROUNDED: every file justified by evidence
- COMPLETE: include every co-edit target declared in
  must_co_edit_relations / dependency_propagation with an action verb
- MINIMAL & SUFFICIENT: smallest change set that fully fixes the defect
- ORDER: list edits in dependency order (dependencies first)
- NO CODE: describe *what* and *why*, not actual code
- TO-BE items in constraints describe behaviors to ADD, not existing ones
- preserved_findings: copy verbatim, never summarize

Return a structured JSON object matching the required schema.
"""


def _backfill_declared_coedit_files(
    plan: PatchPlan, memory: SharedWorkingMemory
) -> list[str]:
    """Ensure every file declared as a co-edit target in evidence is in the plan.

    Language- and framework-agnostic: scans evidence sentences in
    ``must_co_edit_relations`` and ``dependency_propagation``, extracts every
    file path that co-occurs with a co-edit action verb, and appends a
    FileEditPlan for any such path that is missing from ``plan.edits``.

    Returns the list of filepaths that were appended (for logging).  Mutates
    ``plan.edits`` in place.
    """
    existing_paths = {edit.filepath for edit in plan.edits}
    appended: list[str] = []
    seen_new: set[str] = set()

    for path, sentence in _extract_coedit_targets(memory):
        if path in existing_paths or path in seen_new:
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

    return appended


async def _run_patch_planner_async(memory: SharedWorkingMemory) -> PatchPlan:
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
        max_turns=10,
        max_budget_usd=1.5,
    )

    # ── Framework-agnostic co-edit backfill ──
    # Any file path declared in must_co_edit_relations / dependency_propagation
    # with an action verb that is absent from plan.edits is auto-added.
    appended = _backfill_declared_coedit_files(plan, memory)
    if appended:
        print(
            f"[patch-planner] backfilled declared co-edit files: {appended}",
            flush=True,
        )

    # ── Phase 18.D: Ensure preserved_findings populated from findings ──
    # If the model did not fill preserved_findings, backfill from requirements.
    for edit in plan.edits:
        if not edit.preserved_findings:
            for req in memory.evidence_cards.requirements:
                if req.findings:
                    snippets = _extract_prescriptive_snippets(req.findings)
                    for snippet in snippets:
                        if snippet not in edit.preserved_findings:
                            edit.preserved_findings.append(snippet)

    memory.patch_plan = plan
    return plan


def run_patch_planner(memory: SharedWorkingMemory) -> PatchPlan:
    """Synchronous wrapper.

    Args:
        memory: SharedWorkingMemory with evidence cards and cached code.

    Returns:
        PatchPlan with per-file edit intents.
    """
    return asyncio.run(_run_patch_planner_async(memory))
