---
name: inspect-checkpoint
description: "Read-only per-tensor statistics for a saved model checkpoint (.pt/.pkl/.safetensors). Use when the Planner needs to explain WHY a run diverged, plateaued, or collapsed — reports per-layer weight mean/std/min/max/NaN/Inf and flags vanishing/exploding weights. PLANNER-ONLY, read-only."
allowed-tools: Bash, Read, Glob
---

# inspect-checkpoint — Look inside a saved model (Planner-only, read-only)

When a run diverged (NaN), plateaued, or collapsed, the text log rarely says *why*.
This skill opens the checkpoint and reports per-parameter statistics so you can form a
mechanistic hypothesis instead of guessing. It never prints raw weights and never
modifies the file.

## When to use
- The prior cycle's PHENOMENA mentioned NaN/Inf, loss explosion, or a suspicious plateau.
- After the IRM optimizer reset (`update_count == irm_penalty_anneal_iters`) you want to
  check whether the post-reset weights blew up or collapsed.
- To compare a healthy vs unhealthy run's weight scale before proposing an lr / anneal change.

## How to run

The script lives next to this SKILL.md. DomainBed saves `model.pkl` in each split's
output dir. Run this from the release repository root and point the script at it:

```bash
python3 easy-auto-research/scripts/Skills/inspect-checkpoint/scripts/inspect_checkpoint.py <PATH_TO/model.pkl> [--top 20]
```

Use `--top N` to show only the N highest-L2 params (keeps output compact for big nets);
omit for all. Add `--json` for exact numbers to quote.

## What it reports
- Per-tensor: shape, mean, std, min, max, L2 norm, NaN/Inf counts.
- Aggregate diagnostics:
  - **NaN/Inf present** → training diverged; propose a stability fix (lower `lr`,
    later-but-legal anneal, clip/smooth the penalty) — NOT a performance tweak.
  - **std < 1e-4 layers** → possible vanishing/dead layer.
  - **std > 5.0 layers** → possible exploding weights.
  - All-clear message when magnitudes look healthy.

## How to use the output in your plan
- Turn the finding into a concrete, single-hypothesis change with exact values, e.g.
  "post-anneal weights exploded (fc2 std=8.3) → lower `lr` from 1e-3 to 5e-4 this cycle".
- Cite the specific layer names/numbers in your `## Analysis`.

## Hard rules
- **Read-only.** Never edit or delete the checkpoint or its version dir.
- Requires `torch` (already a project dep). Do NOT install packages — if import fails,
  report that and fall back to log-based reasoning.
- Statistics describe symptoms, not root cause — pair with the loss trajectory.
