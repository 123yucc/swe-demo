# API / Relay Adaptations

This file lists every adaptation in this codebase that exists **only** because
we currently route Claude Agent SDK traffic through a third-party
Anthropic-compatible relay (MiniMax) instead of Anthropic's official API.

When migrating back to the official Anthropic API, walk down this file and
revert each item — they are safe to remove and will simplify the codebase.

---

## TL;DR — what to change when going back to official Anthropic

1. Set `.env` to official credentials (no `ANTHROPIC_BASE_URL`, no MiniMax model names).
2. Delete `ANTHROPIC_MODEL` / `ANTHROPIC_FALLBACK_MODEL` from `.env` if you want
   the SDK to use its own default model.
3. Delete `sdk_model_options()` from `src/config.py` and remove all
   `**sdk_model_options(),` spreads in agent files.
4. Replace `src/agents/_structured.py` with a thin wrapper that uses
   the SDK's native `output_format={"type": "json_schema", ...}` again
   (constrained decoding is reliable on Anthropic).  Remove the manual
   parse-and-retry loop.
5. (Optional) Remove `sdk_stderr_logger` if you don't need component-prefixed
   SDK stderr.

---

## Adaptations in detail

### 1. Custom base URL via `.env`

| File | What |
|---|---|
| `.env` | `ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic` |
| `src/config.py` | Reads `ANTHROPIC_BASE_URL`, injects via `sdk_env()` into every `ClaudeAgentOptions(env=...)` |

**Why:** routes SDK → relay → MiniMax instead of SDK → Anthropic API.

**Revert:** delete the `ANTHROPIC_BASE_URL` line from `.env`.  `sdk_env()` will
then inject only the API key and the SDK uses its built-in Anthropic endpoint.

---

### 2. Explicit model name + fallback

| File | What |
|---|---|
| `src/config.py` | `ANTHROPIC_MODEL` / `ANTHROPIC_FALLBACK_MODEL` env vars; `sdk_model_options()` packs them into `model=` / `fallback_model=` for `ClaudeAgentOptions` |
| All agent option blocks (`patch_generator_agent.py`, `_structured.py`) | `**sdk_model_options(),` spread inside `ClaudeAgentOptions(...)` |

**Why:** the relay's Anthropic-compatible endpoint requires a MiniMax model
name (e.g. `MiniMax-M2.7`).  Without `ANTHROPIC_MODEL`, the SDK passes its
default Anthropic name (e.g. `claude-sonnet-...`) and the local `claude` CLI
**rejects MiniMax names AND** the relay rejects Anthropic names — exit code 1
with no useful stderr.  Setting an explicit model is mandatory for the relay
path.

**Revert:** remove the env-var defaults from `src/config.py`, delete
`sdk_model_options()`, and strip `**sdk_model_options(),` from every agent
options block.  The SDK then picks its own default.

---

### 3. Hand-rolled structured-output retry (`src/agents/_structured.py`)

This is the biggest adaptation.

**Native Anthropic** supports `output_format={"type": "json_schema", "schema": ...}`
backed by **constrained decoding** — the model's tokens are forced at
inference time to match the schema, so pydantic validation almost never fails.

**MiniMax relay** does NOT implement constrained decoding.  The schema becomes
a soft hint embedded in the system message; the model freely generates JSON
that frequently violates the schema (wrong enum literal, missing required
field, wrong nesting).  The SDK then runs 3-5 silent retries with the same
prompt, burning tool turns and budget each time, before raising
`error_max_structured_output_retries`.

**Adaptation:** `src/agents/_structured.py:run_structured_query()` bypasses
`output_format` entirely.  It:

1. Renders the pydantic schema into the user prompt as fenced JSON
2. Asks the model to emit a single ```json ... ``` block
3. Extracts JSON from the model's text response
4. Validates with `pydantic.model_validate`
5. **On failure:** re-prompts with the previous reply + `ValidationError` text
   so the model can self-correct (deterministic improvement, unlike the
   SDK's blind retry)
6. Retries up to `max_validation_retries` (default 3, capped at 2 for the
   tool-using deep-search agent and 1 for the tool-using closure-checker
   agent so retries don't re-execute Grep/Read/Glob too many times)

The 4 structured-output agents (`parser`, `deep_search`, `closure_checker`,
`patch_planner`) all delegate to this helper.  Since phase 17 the
closure-checker also uses Grep/Read/Glob (it performs a code-reviewer-style
audit of `evidence_locations` in the repo), so its `max_budget_usd` is 1.5
and `max_turns` is 30 — higher than phase-16's no-tool closure budget.
`run_structured_query` accepts a `cwd=` argument so the tool-using agents
operate from the repo directory (the `evidence_locations` are repo-relative
paths).

`patch_generator_agent.py` does NOT use this helper because it does not need
structured output — it returns a free-text status marker (`PATCH_APPLIED` /
`PATCH_INCOMPLETE`) that the orchestrator inspects.

**Revert:** simplify `_structured.py` so `run_structured_query()` is a thin
shim around the SDK's `output_format=json_schema`:

```python
async def run_structured_query(*, system_prompt, user_prompt, response_model,
                               component, allowed_tools=None, max_turns=10,
                               max_budget_usd=1.0, **_):
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or [],
        permission_mode="acceptEdits",
        max_thinking_tokens=0,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format={"type": "json_schema",
                       "schema": response_model.model_json_schema()},
        env=sdk_env(),
    )
    async for msg in query(prompt=user_prompt, options=options):
        if isinstance(msg, ResultMessage) and msg.structured_output is not None:
            return response_model.model_validate(msg.structured_output)
    raise RuntimeError(f"{component}: no structured output returned.")
```

The agents themselves do not need to change — the helper signature stays
identical.

---

### 4. Component-prefixed SDK stderr (`sdk_stderr_logger`)

| File | What |
|---|---|
| `src/config.py` | `sdk_stderr_logger(component)` factory returning a callable |
| All agent options blocks | `stderr=sdk_stderr_logger("parser"), ...` |

**Why:** the SDK swallows the `claude` CLI's stderr by default.  When the
relay returns an unexpected response shape, the only clue is the CLI's
stderr.  Tagging it with `[sdk-stderr:parser]` etc. makes failures attributable
when the pipeline crosses many sub-agents.

**Revert:** purely diagnostic; safe to keep.  If you choose to remove it,
drop the `sdk_stderr_logger` import and the `stderr=` kwarg from every
agent's options block.

---

## Things that are NOT relay adaptations (do not touch when migrating)

The following look adjacent but are **not** relay-driven and should remain:

- The `evidence.json`, `patch.diff`, `prediction.json`, `patch_outcome.json`
  output filenames in `engine.py` / `main.py` — these are the project's
  on-disk artifact contract and predate any relay work.
- The `RequirementItem`-based architecture in `models/` — Phase 16 domain design.
- The `_enforce_parser_field_whitelist` and `_PARSER_FORBIDDEN_FIELDS` in
  `parser_agent.py` — Phase 16 field-ownership enforcement.
- `DeepSearchBudget`, `check_sufficiency`, `check_correct_attribution`,
  `check_structural_invariants` in `orchestrator/guards.py` — pipeline
  state-machine logic, including Phase 18.A structural invariants (I1/I2/I3).
- `build_audit_manifest()` in `orchestrator/audit.py` — Phase 18.B deterministic
  audit scope calculation.
- `AuditManifest`, `AuditTask`, `AuditResult` models in `models/audit.py` —
  Phase 18.B manifest-driven closure-checker architecture.
- `preserved_findings` field in `FileEditPlan` — Phase 18.D hard-constraint
  propagation from findings to patch-generator.
- Deep-search self-reflection round in `deep_search_agent.py` — Phase 18.E
  token traceability and boundary enumeration checks before returning report.
- Differentiated rework feedback in `_build_per_req_audit_feedback` — Phase 18.F
  failure-type-specific instructions for deep-search rework.

---

## Verification after migration

After reverting per this file, run:

```bash
python -m src.main --instance-json workdir/swe_issue_001/artifacts/instance_metadata.json --repo-dir workdir/swe_issue_001/repo
```

Expected: parser succeeds in a single SDK call (no retry loop messages from
`_structured.py`), deep-search closes within the iteration budget without
`structured-output validation failed` log lines, full pipeline reaches
`PatchSuccess` or `Closed`.
