---
name: analyze-hpo
description: "Read-only hyperparameter/result correlation analysis across Easy-Auto-Research for Deep Learning V* version directories. Use when the Planner needs evidence from prior runs before choosing a hyperparameter. PLANNER-ONLY, read-only."
allowed-tools: Bash, Read, Glob, Grep
---

# analyze-hpo — Data-driven hyperparameter analysis (Planner-only, read-only)

This project-agnostic script scans experiment directories, extracts a required scalar metric and
best-effort hyperparameters, then prints a run table, Pearson correlations, and the best run.
The default directory glob is uppercase `V*`, matching harness-created version directories.

## How to run

Run these commands from the release repository root. Every invocation must name the metric key:

```bash
python3 easy-auto-research/scripts/Skills/analyze-hpo/scripts/analyze_hpo.py \
  --base-dir /path/to/WorkSpace \
  --metric-key val_acc
```

To aggregate a metric across nested sub-runs:

```bash
python3 easy-auto-research/scripts/Skills/analyze-hpo/scripts/analyze_hpo.py \
  --base-dir /path/to/WorkSpace \
  --dir-glob 'V*' \
  --subrun-glob 'train_output_*' \
  --metric-key test_acc \
  --metric-agg last \
  --across-subruns mean \
  --hparam-keys lr,batch_size,weight_decay \
  --json
```

Use `--lower-is-better` for losses or errors. Metric discovery checks JSONL, CSV, then flat JSON
files. Hyperparameters are recovered from common config JSON files and, for requested keys, from
command text in logs.

## What it reports

1. Every matching `V*` run, its scalar metric, optional sub-run values, and recovered hparams.
2. Pearson correlation for numeric hparams with at least three scored runs.
3. The best scored run according to the selected metric direction.

`--json` reports the same three parts as `runs`, `correlations`, and `best_run`.

Only directories named `V<number>_<description>` are analyzed, matching the harness. Malformed
names and symlinked directories are skipped so stray fixtures cannot contaminate the metrics.

## Hard rules

- Read-only: never edit prior version directories.
- Correlation is descriptive, not causal; cite the concrete runs and values used.
- If no matching/scored runs exist, inspect paths and metric keys rather than inventing results.
