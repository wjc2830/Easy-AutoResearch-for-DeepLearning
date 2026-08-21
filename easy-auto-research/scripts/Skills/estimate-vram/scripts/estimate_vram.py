#!/usr/bin/env python3
"""
estimate_vram.py — Project-agnostic, pre-flight VRAM estimate for a training config.

Estimates peak training VRAM = params + gradients + optimizer state (Adam=2x) +
activations, so a planner can avoid proposing a batch size that OOMs. READ-ONLY,
builds the model on CPU only to count params; never launches training or touches the GPU.

Model parameter count comes from ONE of:
  A) --params N                      : you already know the count
  B) --model-adapter "mod:callable"  : callable(**kwargs) returns an nn.Module (or a
                                       (module, ...) tuple / list of modules; all counted)
Adapter imports support --sys-path (repeatable) and --stub-modules (see _shared/_stub.py),
identical to inspect-batch, so no framework is hardcoded.

Activation memory is inherently model-specific; choose how to estimate it:
  --act-bytes-per-sample N   : exact bytes/sample if you know it (best)
  --act-input-shape C H W    : heuristic from input volume (rough, for conv nets)
  (default)                  : heuristic = 4 * param_bytes / batch (very rough)

Examples
--------
python3 estimate_vram.py --params 371394 --batch-size 512 --optimizer adam --gpu-gb 80

python3 estimate_vram.py --model-adapter "mypkg.models:build_net" \
    --model-kwargs '{"width":128}' --sys-path /path/to/repo \
    --batch-size 256 --act-input-shape 3 224 224 --gpu-gb 80
"""
import argparse
import importlib
import json
import os
import sys

_DTYPE_BYTES = {"fp32": 4, "fp16": 2, "bf16": 2}
_OPT_MULT = {"sgd": 0, "momentum": 1, "adam": 2, "adamw": 2}  # extra param-copies


def _human(gb):
    return f"{gb:.3f} GB"


def _count_params_via_adapter(spec, kwargs, sys_paths, stub_spec):
    for p in sys_paths:
        ap_ = os.path.abspath(p)
        if ap_ not in sys.path:
            sys.path.insert(0, ap_)
    if stub_spec:
        shared = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared")
        sys.path.insert(0, os.path.abspath(shared))
        try:
            from _stub import install_stubs
            install_stubs(stub_spec)
        except Exception as e:  # noqa: BLE001
            print(f"[estimate-vram] WARN: could not install stubs ({e}); continuing.",
                  file=sys.stderr)
    if ":" not in spec:
        raise SystemExit(f"[estimate-vram] --model-adapter must be 'module:callable', got {spec!r}")
    mod_name, attr = spec.rsplit(":", 1)
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[estimate-vram] Could not import {mod_name!r}: {e}")
    fn = getattr(mod, attr, None)
    if fn is None:
        raise SystemExit(f"[estimate-vram] {mod_name!r} has no attribute {attr!r}")
    obj = fn(**kwargs)
    modules = obj if isinstance(obj, (list, tuple)) else [obj]
    total = 0
    import torch  # noqa: F401
    for m in modules:
        if hasattr(m, "parameters"):
            total += sum(p.numel() for p in m.parameters())
    if total == 0:
        raise SystemExit("[estimate-vram] adapter produced 0 parameters — did it return an nn.Module?")
    return total


def main():
    ap = argparse.ArgumentParser(description="Project-agnostic pre-flight VRAM estimate")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--params", type=int, help="Known model parameter count")
    src.add_argument("--model-adapter", help="'module:callable' returning an nn.Module")
    ap.add_argument("--model-kwargs", default="{}", help="JSON kwargs for the model adapter")
    ap.add_argument("--sys-path", action="append", default=[])
    ap.add_argument("--stub-modules", default=None)
    ap.add_argument("--batch-size", type=int, required=True)
    ap.add_argument("--optimizer", choices=list(_OPT_MULT), default="adam")
    ap.add_argument("--dtype", choices=list(_DTYPE_BYTES), default="fp32")
    ap.add_argument("--act-bytes-per-sample", type=int, default=None)
    ap.add_argument("--act-input-shape", type=int, nargs="+", default=None,
                    help="C H W ... to estimate activations for conv nets (heuristic)")
    ap.add_argument("--act-multiplier", type=float, default=64.0,
                    help="Feature-map multiplier for the input-shape heuristic (default 64)")
    ap.add_argument("--gpu-gb", type=float, default=None)
    ap.add_argument("--safety", type=float, default=1.3)
    ap.add_argument("--max-batch", type=int, default=None,
                    help="Optional hard ceiling on suggested batch size (project constraint)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.params is not None:
        P = args.params
    else:
        try:
            kw = json.loads(args.model_kwargs)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[estimate-vram] --model-kwargs not valid JSON: {e}")
        P = _count_params_via_adapter(args.model_adapter, kw, args.sys_path, args.stub_modules)

    b = _DTYPE_BYTES[args.dtype]
    param_bytes = P * b
    grad_bytes = P * b
    opt_bytes = P * b * _OPT_MULT[args.optimizer]

    if args.act_bytes_per_sample is not None:
        act_per_sample = args.act_bytes_per_sample
    elif args.act_input_shape:
        vol = 1
        for d in args.act_input_shape:
            vol *= d
        act_per_sample = vol * b * args.act_multiplier
    else:
        act_per_sample = (4 * param_bytes) / max(1, args.batch_size)  # very rough fallback
    act_bytes = act_per_sample * args.batch_size

    subtotal = param_bytes + grad_bytes + opt_bytes + act_bytes
    total = subtotal * args.safety
    to_gb = lambda x: x / (1024 ** 3)  # noqa: E731

    report = {
        "params": P, "batch_size": args.batch_size, "optimizer": args.optimizer,
        "dtype": args.dtype, "param_gb": to_gb(param_bytes), "grad_gb": to_gb(grad_bytes),
        "optimizer_state_gb": to_gb(opt_bytes), "activation_gb": to_gb(act_bytes),
        "subtotal_gb": to_gb(subtotal), "safety": args.safety,
        "estimated_peak_gb": to_gb(total), "gpu_gb": args.gpu_gb,
        "verdict": "unknown", "headroom_gb": None, "over_by_gb": None,
        "suggested_batch_size": None,
    }
    if args.gpu_gb is not None:
        if report["estimated_peak_gb"] > args.gpu_gb:
            report["verdict"] = "likely_oom"
            report["over_by_gb"] = report["estimated_peak_gb"] - args.gpu_gb
            avail_for_act = (args.gpu_gb * (1024 ** 3) / args.safety
                             - (param_bytes + grad_bytes + opt_bytes))
            safe_bs = max(1, int(avail_for_act / act_per_sample)) if act_per_sample > 0 else args.batch_size
            if args.max_batch is not None:
                safe_bs = min(safe_bs, args.max_batch)
            report["suggested_batch_size"] = safe_bs
        else:
            report["verdict"] = "fits"
            report["headroom_gb"] = args.gpu_gb - report["estimated_peak_gb"]

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"\n=== VRAM estimate — {P:,} params, batch={args.batch_size}, "
          f"{args.optimizer}/{args.dtype} ===\n")
    print(f"  parameters         : {_human(report['param_gb'])}")
    print(f"  gradients          : {_human(report['grad_gb'])}")
    print(f"  optimizer state    : {_human(report['optimizer_state_gb'])}  "
          f"({args.optimizer} = {_OPT_MULT[args.optimizer]}x params)")
    print(f"  activations (est.) : {_human(report['activation_gb'])}  (scales with batch)")
    print(f"  subtotal           : {_human(report['subtotal_gb'])}")
    print(f"  + {args.safety:.1f}x safety     : {_human(report['estimated_peak_gb'])}  "
          f"<-- estimated peak")

    print("\n=== Verdict ===")
    if args.gpu_gb is not None:
        if report["verdict"] == "likely_oom":
            print(f"  LIKELY OOM: est. peak {_human(report['estimated_peak_gb'])} > "
                  f"{args.gpu_gb:.0f} GB by {_human(report['over_by_gb'])}.")
            print(f"     try batch_size approximately {report['suggested_batch_size']}"
                  f"{f' (at most your --max-batch {args.max_batch})' if args.max_batch is not None else ''}, "
                  f"or enable gradient checkpointing.")
        else:
            print(f"  FITS: est. peak {_human(report['estimated_peak_gb'])} within "
                  f"{args.gpu_gb:.0f} GB ({_human(report['headroom_gb'])} headroom).")
    else:
        print(f"  (pass --gpu-gb for a fit/OOM verdict; estimated peak "
              f"{_human(report['estimated_peak_gb'])}.)")
    print("\n  NOTE: estimate only — real peak depends on framework overhead, cuDNN "
          "workspace, and fragmentation. Activation term is the least precise part.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
