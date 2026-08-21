---
name: estimate-vram
description: "Read-only, project-agnostic pre-flight VRAM estimate from a known parameter count or a user-supplied model adapter. PLANNER-ONLY, read-only, no GPU."
allowed-tools: Bash, Read, Glob
---

# estimate-vram — Pre-flight memory estimate (Planner-only, read-only)

Use this skill before proposing a configuration likely to increase model-state or activation
memory, or after an out-of-memory failure. The estimate is approximate and never launches
training or touches a GPU.

## Parameter source

Choose exactly one source:

- `--params N` when the parameter count is known; or
- `--model-adapter module:callable`, where the callable accepts `--model-kwargs` JSON and
  returns an `nn.Module`, a `(module, ...)` tuple, or a list of modules.

For adapter imports, `--sys-path PATH` is repeatable and `--stub-modules SPEC` can stub optional
heavy modules.

## How to run

Run these commands from the release repository root. Known parameter count:

```bash
python3 easy-auto-research/scripts/Skills/estimate-vram/scripts/estimate_vram.py \
  --params 500000000 \
  --batch-size 64 \
  --optimizer adamw \
  --dtype bf16 \
  --act-bytes-per-sample 104857600 \
  --gpu-gb 80 \
  --json
```

Project model adapter:

```bash
python3 easy-auto-research/scripts/Skills/estimate-vram/scripts/estimate_vram.py \
  --sys-path /path/to/project \
  --model-adapter 'project.models:build_model' \
  --model-kwargs '{"width":128}' \
  --batch-size 32 \
  --act-input-shape 3 224 224 \
  --gpu-gb 80
```

Use `--act-bytes-per-sample` when measured data is available. Otherwise
`--act-input-shape ...` uses a rough feature-map heuristic; if neither is supplied, the fallback
is rougher still. `--max-batch N` only caps the suggested safe batch size.

```bash
python3 easy-auto-research/scripts/Skills/estimate-vram/scripts/estimate_vram.py --help
```

## What it reports

- parameters, gradients, optimizer state, and estimated activation memory;
- a safety-multiplied peak estimate; and
- when `--gpu-gb` is supplied, a fit/OOM heuristic and possible batch-size suggestion.

`--json` reports the same verdict as `verdict` (`fits`, `likely_oom`, or `unknown` without
`--gpu-gb`), with `headroom_gb`, `over_by_gb`, and `suggested_batch_size`.

## Hard rules

- Read-only and CPU-only: do not launch training or install packages.
- Treat the result as a planning estimate, not a measurement; framework overhead, workspaces,
  sharding, mixed-precision policy, and fragmentation can change real peak memory.
