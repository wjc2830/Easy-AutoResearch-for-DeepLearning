---
name: inspect-batch
description: "Read-only, project-agnostic inspection of one batch returned by a user-supplied Python adapter. Reports input shape, dtype, value range, per-channel statistics, and label distribution. PLANNER-ONLY, read-only, CPU-only."
allowed-tools: Bash, Read, Glob
---

# inspect-batch — Inspect one real batch (Planner-only, read-only)

Use this skill when a training result suggests a data-loading, normalization, shape, channel,
or label-distribution problem. The script does not know how a project constructs its dataset;
you supply a Python adapter callable.

## Adapter contract

`--adapter module:callable` imports a callable that accepts the JSON object supplied by
`--adapter-kwargs` and returns one of:

- a `torch.utils.data.DataLoader`;
- a `torch.utils.data.Dataset`; or
- a sequence of datasets/loaders, in which case `--index N` is required.

A batch may be `(x, y)` or `(x, y, ...)`. For another batch structure, provide an adapter that
normalizes it to this form. Use repeatable `--sys-path PATH` options for code that is not
installed. `--stub-modules SPEC` can stub optional heavy imports; see `scripts/Skills/_shared/_stub.py`.

## How to run

Run these commands from the release repository root:

```bash
python3 easy-auto-research/scripts/Skills/inspect-batch/scripts/inspect_batch.py \
  --sys-path /path/to/project \
  --adapter 'project.data:make_dataset' \
  --adapter-kwargs '{"root":"/path/to/data","split":"train"}' \
  --index 0 \
  --batch-size 64 \
  --json
```

Omit `--index` when the adapter returns a single dataset or loader. Inspect the exact interface
before adapting a project:

```bash
python3 easy-auto-research/scripts/Skills/inspect-batch/scripts/inspect_batch.py --help
```

## What it reports

- selected adapter/index and observed batch size;
- input tensor shape, dtype, range, mean, and standard deviation;
- per-channel statistics when the second dimension is reasonably small;
- label shape and integer-label counts when available; and
- heuristic warnings for suspicious ranges or severe imbalance.

`--json` includes the same heuristics under `diagnostics`, plus `label_imbalance_ratio` when
integer labels are present.

## Hard rules

- Read-only and CPU-only: do not train, write project data, or install packages.
- The adapter is project-specific; read the target data API before choosing its callable and kwargs.
- One shuffled batch is a sample, not the full distribution.
