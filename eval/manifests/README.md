# Experiment manifests

This directory contains maintained experiment entry points. Historical
`eval/local_manifest*.json` files remain in place for reproducibility but are
not the source of truth for new full-matrix runs.

## Four-model SWE-bench Pro run

`swebench-pro-731.four-models.json` defines the shared cost policy and the four
model variants. Its `issues: "all"` value discovers prepared `swe_issue_*`
directories and `expected_issue_count: 731` fails closed if the corpus is
incomplete.

Freeze it into one manifest per model before running:

```bash
python scripts/fetch_issues.py --all --start 0 --start-label 1 \
  --source-jsonl eval/SWE-bench_Pro-os/helper_code/sweap_eval_full_v2.jsonl

python scripts/prepare_harness_matrix.py \
  --manifest eval/manifests/swebench-pro-731.four-models.json
```

The case preparer stores one shared bare mirror per upstream repository under
`workdir/_repo_cache`. Case working trees use Git alternates, avoiding hundreds
of duplicate object databases while retaining independent files and commits.

Generated files live under:

```text
runtime/experiments/swebench-pro-731-four-models-cost-v1/
  matrix.json
  manifests/<model>.json
```

Run every model sequentially at the experiment level (each model still uses
the configured per-case parallelism):

```bash
bash scripts/run_harness_matrix.sh \
  runtime/experiments/swebench-pro-731-four-models-cost-v1
```

Run each frozen single-model manifest through the staged runner. Use names such
as `swebench-pro-731-four-models-cost-v1/<model>/<phase>` so runtime logs stay
under one experiment namespace in `logs/runs/`.

## Cost policy

Shared controls apply to both backends: evidence structural deduplication,
single-item Deep Search scheduling, batched dynamic closure, bounded Read/Grep
requests, and model-call metrics. OpenAI profiles additionally enable Responses
prompt caching and output clipping. Anthropic uses a hybrid path: tool-free
structured calls use the Messages API with an explicit ephemeral cache marker
on the stable system prefix, while tool-using calls retain the Agent SDK loop.
Claude Code also performs prompt caching internally; both paths report cache
creation/read tokens into the same metrics.

AgentDiet is not enabled in the production manifest. It rewrites prior steps in
a persistent Trae trajectory using another model or LLMLingua. This harness uses
isolated structured sub-agent calls, and Claude's inner Agent SDK transcript is
not mutable through the public API. Applying AgentDiet there would require a
different agent loop and is not a safe drop-in optimization.
