#!/usr/bin/env python3
"""
inspect_batch.py — Project-agnostic single-batch inspection of a data pipeline.

Pulls ONE batch from a dataset/dataloader and reports tensor shape, dtype, value range,
per-channel mean/std, and label distribution. Surfaces normalization bugs, channel
conventions, and class imbalance that a text log never shows. READ-ONLY, CPU-only.

Because "how to build the dataset" is inherently project-specific, you supply an ADAPTER:
a Python callable that returns something batch-able. Nothing here hardcodes a framework.

Adapter contract
----------------
--adapter "mymod:make_loader"   where make_loader(**kwargs) returns ONE of:
    * a torch.utils.data.DataLoader           -> first batch is used directly
    * a torch.utils.data.Dataset              -> wrapped in a DataLoader(batch_size,shuffle)
    * a (dataset_or_loader) also accepted if it's indexable list-of-datasets: pass --index N
Adapter kwargs come from --adapter-kwargs (JSON), e.g. '{"root":"/data","env":2}'.
A batch is expected to be (x, y) or (x, y, *rest); x is the input tensor, y the labels.

Import support (so you can point at an uninstalled-in-site codebase):
    --sys-path /path/to/repo        (repeatable) prepend to sys.path before importing
    --stub-modules "timm,wilds.datasets.foo:BarDataset"  stub heavy deps (see _shared/_stub.py)

Examples
--------
# Generic dataset via a tiny adapter supplied by the target project:
python3 inspect_batch.py --adapter project.data:make_dataset \
    --sys-path /path/to/project --adapter-kwargs '{"root":"/data","split":"train"}'

# A DomainBed-style dataset that returns a LIST of per-env datasets, pick env 2:
python3 inspect_batch.py --sys-path /path/to/codebase \
    --stub-modules "timm,wilds.datasets.camelyon17_dataset:Camelyon17Dataset,wilds.datasets.fmow_dataset:FMoWDataset" \
    --adapter domainbed.datasets:ColoredMNIST \
    --adapter-kwargs '{"root":"/data/MNIST","test_envs":[],"hparams":{"data_augmentation":false,"class_balanced":false}}' \
    --index 2 --batch-size 64
"""
import argparse
import importlib
import json
import os
import sys


def _load_adapter(spec):
    if ":" not in spec:
        raise SystemExit(f"[inspect-batch] --adapter must be 'module:callable', got {spec!r}")
    mod_name, attr = spec.rsplit(":", 1)
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[inspect-batch] Could not import adapter module {mod_name!r}: {e}")
    if not hasattr(mod, attr):
        raise SystemExit(f"[inspect-batch] {mod_name!r} has no attribute {attr!r}")
    return getattr(mod, attr)


def _to_loader(obj, index, batch_size):
    import torch  # noqa: F401
    from torch.utils.data import DataLoader, Dataset
    # list/tuple of datasets (e.g. one per domain/env) -> pick by index
    if isinstance(obj, (list, tuple)) or (hasattr(obj, "__getitem__") and hasattr(obj, "__len__")
                                          and not isinstance(obj, Dataset)):
        if index is None:
            raise SystemExit("[inspect-batch] adapter returned a sequence of datasets; "
                             "pass --index N to choose one.")
        obj = obj[index]
    if isinstance(obj, DataLoader):
        return obj
    if isinstance(obj, Dataset) or hasattr(obj, "__getitem__"):
        return DataLoader(obj, batch_size=batch_size, shuffle=True, num_workers=0)
    raise SystemExit(f"[inspect-batch] adapter returned unusable type {type(obj)}; "
                     f"expected DataLoader / Dataset / sequence of datasets.")


def main():
    ap = argparse.ArgumentParser(description="Project-agnostic single-batch inspection")
    ap.add_argument("--adapter", required=True,
                    help="'module:callable' returning a DataLoader/Dataset/sequence-of-datasets")
    ap.add_argument("--adapter-kwargs", default="{}", help="JSON kwargs passed to the adapter")
    ap.add_argument("--index", type=int, default=None,
                    help="If the adapter returns a sequence of datasets, pick this index")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--sys-path", action="append", default=[],
                    help="Path(s) to prepend to sys.path before import (repeatable)")
    ap.add_argument("--stub-modules", default=None,
                    help="Comma spec of heavy modules to stub (see _shared/_stub.py)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    for p in args.sys_path:
        ap_ = os.path.abspath(p)
        if ap_ not in sys.path:
            sys.path.insert(0, ap_)

    if args.stub_modules:
        # locate the shared stub helper next to this skill
        shared = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared")
        sys.path.insert(0, os.path.abspath(shared))
        try:
            from _stub import install_stubs
            install_stubs(args.stub_modules)
        except Exception as e:  # noqa: BLE001
            print(f"[inspect-batch] WARN: could not install stubs ({e}); continuing.", file=sys.stderr)

    try:
        import torch  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[inspect-batch] torch not importable: {e}")

    try:
        kwargs = json.loads(args.adapter_kwargs)
    except json.JSONDecodeError as e:
        raise SystemExit(f"[inspect-batch] --adapter-kwargs is not valid JSON: {e}")

    adapter = _load_adapter(args.adapter)
    try:
        obj = adapter(**kwargs)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[inspect-batch] adapter call failed: {e}")

    loader = _to_loader(obj, args.index, args.batch_size)
    batch = next(iter(loader))
    if isinstance(batch, (list, tuple)):
        x = batch[0]
        y = batch[1] if len(batch) > 1 else None
    else:
        x, y = batch, None

    import torch
    xf = x.detach().float()
    report = {
        "adapter": args.adapter,
        "index": args.index,
        "batch_size": int(x.shape[0]) if hasattr(x, "shape") else None,
        "x_shape": list(x.shape),
        "x_dtype": str(x.dtype),
        "x_min": float(xf.min()),
        "x_max": float(xf.max()),
        "x_mean": float(xf.mean()),
        "x_std": float(xf.std()),
    }
    if xf.dim() >= 2:
        C = xf.shape[1]
        if C <= 16:  # per-channel only makes sense for reasonable channel counts
            report["per_channel_mean"] = [round(float(xf[:, c].mean()), 4) for c in range(C)]
            report["per_channel_std"] = [round(float(xf[:, c].std()), 4) for c in range(C)]

    dist = None
    if y is not None:
        yv = y.detach().flatten().tolist()
        # only build a distribution for integer-like labels
        if all(float(v).is_integer() for v in yv[:256]):
            dist = {}
            for v in yv:
                dist[int(v)] = dist.get(int(v), 0) + 1
            dist = dict(sorted(dist.items()))
        report["label_shape"] = list(y.shape)
        report["label_counts"] = dist

    diagnostics = []
    if report["x_max"] > 1.5 and report["x_min"] >= 0:
        diagnostics.append({
            "level": "warning",
            "message": (f"x max = {report['x_max']:.2f} > 1.5 with min >= 0; inputs may "
                        "not be normalized to [0,1]. Check preprocessing."),
        })
    elif report["x_min"] < -5 or report["x_max"] > 5:
        diagnostics.append({
            "level": "warning",
            "message": (f"wide input range [{report['x_min']:.2f}, {report['x_max']:.2f}]; "
                        "confirm this is the intended normalization."),
        })
    else:
        diagnostics.append({
            "level": "ok",
            "message": (f"input range looks reasonable "
                        f"([{report['x_min']:.2f}, {report['x_max']:.2f}])."),
        })
    if dist:
        counts = list(dist.values())
        imbalance = max(counts) / max(1, min(counts))
        report["label_imbalance_ratio"] = imbalance
        if imbalance > 3:
            diagnostics.append({
                "level": "warning",
                "message": (f"label imbalance ratio {imbalance:.1f}x in this batch "
                            f"(max {max(counts)} vs min {min(counts)})."),
            })
        else:
            diagnostics.append({
                "level": "ok",
                "message": f"labels roughly balanced in this batch (ratio {imbalance:.1f}x).",
            })
    report["diagnostics"] = diagnostics
    report["note"] = "This is one random batch; treat its distribution as indicative, not exact."

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"\n=== inspect-batch: adapter={args.adapter}"
          f"{f' index={args.index}' if args.index is not None else ''}, "
          f"batch_size={args.batch_size} ===\n")
    print(f"  x shape            : {report['x_shape']}  dtype={report['x_dtype']}")
    print(f"  x value range      : [{report['x_min']:.4f}, {report['x_max']:.4f}]")
    print(f"  x mean / std       : {report['x_mean']:.4f} / {report['x_std']:.4f}")
    if "per_channel_mean" in report:
        print(f"  per-channel mean   : {report['per_channel_mean']}")
        print(f"  per-channel std    : {report['per_channel_std']}")
    if y is not None:
        print(f"  label shape        : {report.get('label_shape')}")
        print(f"  label distribution : {report.get('label_counts') or '(non-integer labels)'}")

    print("\n=== Diagnostics ===")
    for diagnostic in diagnostics:
        print(f"  {diagnostic['level'].upper()}: {diagnostic['message']}")
    print(f"\n  NOTE: {report['note']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
