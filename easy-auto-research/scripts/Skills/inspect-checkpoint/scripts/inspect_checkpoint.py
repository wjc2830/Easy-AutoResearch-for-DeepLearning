#!/usr/bin/env python3
"""
inspect_checkpoint.py — Read-only per-tensor statistics for a saved model checkpoint.

Loads a .pt / .pkl / .pth / .safetensors checkpoint and prints per-parameter summary
statistics (shape, mean, std, min, max, NaN/Inf counts, L2 norm) plus aggregate flags
for vanishing / exploding weights. Does NOT print raw tensors (would flood context) and
NEVER modifies the file.

This is a PLANNER diagnostic aid. It only reads.

Usage:
    python3 inspect_checkpoint.py <checkpoint_path> [--top 20] [--json]

Notes:
    - DomainBed saves `model.pkl` (a dict with "model_dict" state_dict + "args"/"hparams").
      This script unwraps common container keys automatically.
    - Requires torch to be importable (already a project dependency). For .safetensors,
      requires the `safetensors` package; otherwise it degrades gracefully.
"""
import argparse
import json
import os
import sys


def _load_state_dict(path, allow_unsafe_pickle=False):
    """Return (state_dict, meta) where state_dict maps name->tensor. meta is any
    non-tensor top-level info we can surface (args/hparams)."""
    meta = {}
    if path.endswith(".safetensors"):
        try:
            from safetensors.torch import load_file
        except ImportError:
            raise SystemExit("[inspect-checkpoint] safetensors not installed; cannot read "
                             f"{path}. Install safetensors or point at a .pt/.pkl file.")
        return load_file(path), meta

    import torch
    obj = torch.load(path, map_location="cpu", weights_only=not allow_unsafe_pickle)

    # Unwrap common containers
    if isinstance(obj, dict):
        for k in ("args", "hparams", "model_hparams", "model_num_domains"):
            if k in obj and not _looks_like_tensor(obj[k]):
                meta[k] = _jsonable(obj[k])
        for key in ("model_dict", "state_dict", "model", "net", "model_state_dict"):
            if key in obj and isinstance(obj[key], dict):
                return obj[key], meta
        # maybe obj itself is already a state_dict of tensors
        if any(_looks_like_tensor(v) for v in obj.values()):
            return obj, meta
    # a bare module?
    if hasattr(obj, "state_dict"):
        return obj.state_dict(), meta
    raise SystemExit(f"[inspect-checkpoint] Could not find a state_dict inside {path}.")


def _looks_like_tensor(v):
    return hasattr(v, "shape") and hasattr(v, "dtype") and hasattr(v, "float")


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _stats(t):
    import torch
    tf = t.detach().float()
    n_nan = int(torch.isnan(tf).sum())
    n_inf = int(torch.isinf(tf).sum())
    finite = tf[torch.isfinite(tf)]
    if finite.numel() == 0:
        return {"shape": list(t.shape), "numel": t.numel(), "nan": n_nan, "inf": n_inf,
                "mean": None, "std": None, "min": None, "max": None, "l2": None}
    return {
        "shape": list(t.shape),
        "numel": int(t.numel()),
        "nan": n_nan,
        "inf": n_inf,
        "mean": float(finite.mean()),
        "std": float(finite.std()) if finite.numel() > 1 else 0.0,
        "min": float(finite.min()),
        "max": float(finite.max()),
        "l2": float(finite.norm(2)),
    }


def main():
    ap = argparse.ArgumentParser(description="Read-only checkpoint tensor statistics")
    ap.add_argument("checkpoint", help="Path to .pt/.pkl/.pth/.safetensors")
    ap.add_argument("--top", type=int, default=0,
                    help="Only show the N params with the largest L2 norm (0 = all)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-unsafe-pickle", action="store_true",
                    help="Allow arbitrary-code pickle deserialization for trusted checkpoints")
    args = ap.parse_args()

    if not os.path.isfile(args.checkpoint):
        raise SystemExit(f"[inspect-checkpoint] Not a file: {args.checkpoint}")

    sd, meta = _load_state_dict(args.checkpoint, args.allow_unsafe_pickle)
    rows = []
    for name, t in sd.items():
        if not _looks_like_tensor(t):
            continue
        s = _stats(t)
        s["name"] = name
        rows.append(s)

    if not rows:
        raise SystemExit("[inspect-checkpoint] No tensor parameters found.")

    total_nan = sum(r["nan"] for r in rows)
    total_inf = sum(r["inf"] for r in rows)
    stds = [r["std"] for r in rows if r["std"] is not None]

    if args.json:
        print(json.dumps({"meta": meta, "params": rows,
                          "total_nan": total_nan, "total_inf": total_inf}, indent=2))
        return 0

    show = sorted(rows, key=lambda r: (r["l2"] or 0), reverse=True)
    if args.top > 0:
        show = show[:args.top]

    print(f"\n=== Checkpoint: {args.checkpoint} ===")
    if meta:
        print(f"meta: {json.dumps(meta)[:400]}")
    print(f"{len(rows)} tensor params | total NaN={total_nan} Inf={total_inf}\n")
    hdr = f"{'param':<45} {'shape':>18} {'mean':>10} {'std':>10} {'min':>10} {'max':>10} {'L2':>10} {'nan/inf':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in show:
        def f(x):
            return f"{x:>10.4g}" if isinstance(x, float) else f"{'--':>10}"
        shp = "x".join(map(str, r["shape"]))
        ni = f"{r['nan']}/{r['inf']}"
        print(f"{r['name']:<45} {shp:>18} {f(r['mean'])} {f(r['std'])} "
              f"{f(r['min'])} {f(r['max'])} {f(r['l2'])} {ni:>8}")

    # ---- diagnostic flags ----
    print("\n=== Diagnostics ===")
    if total_nan or total_inf:
        print(f"  ⚠️  NON-FINITE weights: {total_nan} NaN, {total_inf} Inf — "
              f"training diverged. Prioritize a STABILITY fix (lower lr / earlier-but->=100 "
              f"anneal / gradient clipping in the penalty), not a performance tweak.")
    if stds:
        tiny = [r["name"] for r in rows if r["std"] is not None and r["std"] < 1e-4]
        huge = [r["name"] for r in rows if r["std"] is not None and r["std"] > 5.0]
        if tiny:
            print(f"  ⚠️  {len(tiny)} params with std<1e-4 (possible dead/vanishing layer): "
                  f"{tiny[:5]}{' ...' if len(tiny) > 5 else ''}")
        if huge:
            print(f"  ⚠️  {len(huge)} params with std>5.0 (possible exploding weights): "
                  f"{huge[:5]}{' ...' if len(huge) > 5 else ''}")
        if not tiny and not huge and not (total_nan or total_inf):
            print("  ✅ Weight magnitudes look healthy (no NaN/Inf, no vanishing/exploding "
                  "std outliers).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
