#!/usr/bin/env python3
"""
analyze_hpo.py — Project-agnostic hyperparameter/result correlation across experiment dirs.

Scans a set of experiment directories (default: Easy-Auto-Research for Deep Learning-style `V*/` version dirs, but any
glob works), extracts for each run: (a) the hyperparameters it used and (b) a scalar target
metric, then prints a comparison table + Pearson correlations of each numeric hparam with
the metric, and the best run so far.

Nothing here is tied to a particular framework. You tell it where metrics/hparams live via
flags; sensible autodetection covers the common cases (results.jsonl, metrics.csv, JSON).

READ-ONLY: never writes, edits, launches, or kills anything.

Usage examples
--------------
# Generic: metric is the max of column/key "val_acc"; hparams pulled from any json/yaml in the dir
python3 analyze_hpo.py --base-dir . --metric-key val_acc --metric-agg max

# Only look at V*/ dirs, minimize a loss, hparams limited to a known set
python3 analyze_hpo.py --base-dir . --dir-glob 'V*' \
    --metric-key val/loss --metric-agg min --lower-is-better \
    --hparam-keys lr,batch_size,weight_decay

# Aggregate a metric that is spread across sub-run dirs (e.g. one dir per split),
# taking the MEAN of each sub-run's final value:
python3 analyze_hpo.py --base-dir . --dir-glob 'V*' \
    --subrun-glob 'train_output_*' --metric-key 'test_acc' \
    --metric-agg last --across-subruns mean

Metric extraction order inside a (sub)run dir:
  1. results.jsonl / *.jsonl  (per-line JSON; take the metric key across lines)
  2. metrics.csv / *.csv      (header column = metric key)
  3. *.json                   (flat dict; single scalar under metric key)
The `--metric-agg` (max|min|last|mean) reduces multiple logged values to one scalar per
(sub)run; `--across-subruns` (mean|max|min|sum) then reduces across sub-run dirs.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys


# ----------------------------- metric extraction -----------------------------

def _agg(vals, how):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    if how == "max":
        return max(vals)
    if how == "min":
        return min(vals)
    if how == "last":
        return vals[-1]
    if how == "mean":
        return sum(vals) / len(vals)
    if how == "sum":
        return sum(vals)
    return vals[-1]


def _floaty(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _metric_from_jsonl(path, key):
    vals = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and key in row:
                    v = _floaty(row[key])
                    if v is not None:
                        vals.append(v)
    except OSError:
        pass
    return vals


def _metric_from_csv(path, key):
    vals = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and key in reader.fieldnames:
                for row in reader:
                    v = _floaty(row.get(key))
                    if v is not None:
                        vals.append(v)
    except OSError:
        pass
    return vals


def _metric_from_json(path, key):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and key in data:
        v = _floaty(data[key])
        return [v] if v is not None else []
    return []


def extract_metric(run_dir, key, agg):
    """Return one scalar for this run dir by scanning known metric file types."""
    # 1) jsonl
    for p in sorted(glob.glob(os.path.join(run_dir, "**", "*.jsonl"), recursive=True)):
        vals = _metric_from_jsonl(p, key)
        if vals:
            return _agg(vals, agg)
    # 2) csv
    for p in sorted(glob.glob(os.path.join(run_dir, "**", "*.csv"), recursive=True)):
        vals = _metric_from_csv(p, key)
        if vals:
            return _agg(vals, agg)
    # 3) flat json (skip obvious hparam files handled elsewhere)
    for p in sorted(glob.glob(os.path.join(run_dir, "**", "*.json"), recursive=True)):
        vals = _metric_from_json(p, key)
        if vals:
            return _agg(vals, agg)
    return None


# ----------------------------- hparam extraction -----------------------------

def extract_hparams(run_dir, wanted_keys=None):
    """Best-effort: merge any JSON dict that looks like config/hparams, then fall back to
    scraping a `--key value` / `key=value` pattern from logs for wanted_keys."""
    hp = {}
    json_candidates = []
    for pat in ("**/hparams.json", "**/config.json", "**/args.json",
                "**/*hparams*.json", "**/*config*.json", "**/*args*.json"):
        json_candidates += glob.glob(os.path.join(run_dir, pat), recursive=True)
    for p in sorted(set(json_candidates)):
        try:
            with open(p) as f:
                data = json.load(f)
            if isinstance(data, dict):
                # flatten one level of a common {"hparams": {...}} nesting
                if "hparams" in data and isinstance(data["hparams"], dict):
                    hp.update(data["hparams"])
                hp.update({k: v for k, v in data.items()
                           if not isinstance(v, (dict, list))})
        except (OSError, json.JSONDecodeError):
            pass

    # scrape logs for still-missing wanted keys
    if wanted_keys:
        missing = [k for k in wanted_keys if k not in hp]
        if missing:
            logs = (glob.glob(os.path.join(run_dir, "**", "*.txt"), recursive=True) +
                    glob.glob(os.path.join(run_dir, "**", "*.log"), recursive=True) +
                    glob.glob(os.path.join(run_dir, "**", "*.out"), recursive=True))
            # also try a --hparams '{...}' JSON blob (common in argparse CLIs)
            for p in logs:
                try:
                    with open(p, errors="ignore") as f:
                        txt = f.read()
                except OSError:
                    continue
                m = (re.search(r"--hparams\s+'(\{.*?\})'", txt) or
                     re.search(r'--hparams\s+"(\{.*?\})"', txt))
                if m:
                    try:
                        blob = json.loads(m.group(1))
                        for k in missing:
                            if k in blob:
                                hp[k] = blob[k]
                    except json.JSONDecodeError:
                        pass
                for k in list(missing):
                    if k in hp:
                        continue
                    mm = (re.search(rf"--{re.escape(k)}[= ]+([^\s'\"]+)", txt) or
                          re.search(rf"\b{re.escape(k)}\s*[=:]\s*([^\s,'\"}}]+)", txt))
                    if mm:
                        hp[k] = mm.group(1)
    if wanted_keys:
        return {k: hp[k] for k in wanted_keys if k in hp}
    return hp


# ----------------------------- correlation -----------------------------

def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _is_number(v):
    return _floaty(v) is not None


# ----------------------------- main -----------------------------

def main():
    ap = argparse.ArgumentParser(description="Project-agnostic HPO correlation across run dirs")
    ap.add_argument("--base-dir", required=True, help="Root that contains the run dirs")
    ap.add_argument("--dir-glob", default="V*",
                    help="Glob (relative to base-dir) selecting run dirs (default: 'V*')")
    ap.add_argument("--subrun-glob", default=None,
                    help="Optional glob inside each run dir for sub-runs (e.g. per-split "
                         "dirs). If set, the metric is extracted per sub-run then reduced "
                         "with --across-subruns.")
    ap.add_argument("--metric-key", required=True,
                    help="Key/column name of the target scalar metric")
    ap.add_argument("--metric-agg", default="last", choices=["max", "min", "last", "mean", "sum"],
                    help="Reduce multiple logged metric values within a (sub)run to one scalar")
    ap.add_argument("--across-subruns", default="mean",
                    choices=["mean", "max", "min", "sum"],
                    help="Reduce per-sub-run scalars into one run scalar (only with --subrun-glob)")
    ap.add_argument("--lower-is-better", action="store_true",
                    help="Metric is a loss/error (best run = min). Default: higher is better.")
    ap.add_argument("--hparam-keys", default=None,
                    help="Comma-separated hparam names to track/correlate. If omitted, all "
                         "scalar keys found in config/hparam JSONs are used.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    base = os.path.abspath(args.base_dir)
    wanted = ([k.strip() for k in args.hparam_keys.split(",") if k.strip()]
              if args.hparam_keys else None)

    run_dirs = sorted(
        d for d in glob.glob(os.path.join(base, args.dir_glob))
        if (not os.path.islink(d)
            and os.path.isdir(d)
            and re.fullmatch(r"V[1-9][0-9]*_[A-Za-z0-9][A-Za-z0-9_-]*", os.path.basename(d)))
    )
    if not run_dirs:
        print(f"[analyze-hpo] No run dirs matching '{args.dir_glob}' under {base}. "
              f"Nothing to analyze yet.")
        return 0

    records = []
    for rd in run_dirs:
        if args.subrun_glob:
            subs = sorted(d for d in glob.glob(os.path.join(rd, "**", args.subrun_glob),
                                               recursive=True) if os.path.isdir(d))
            sub_scalars = {os.path.relpath(s, rd): extract_metric(s, args.metric_key, args.metric_agg)
                           for s in subs}
            metric = _agg([v for v in sub_scalars.values() if v is not None],
                          args.across_subruns)
        else:
            sub_scalars = None
            metric = extract_metric(rd, args.metric_key, args.metric_agg)
        records.append({
            "run": os.path.basename(rd),
            "metric": metric,
            "subruns": sub_scalars,
            "hparams": extract_hparams(rd, wanted),
        })

    # Derive the same summary before selecting human-readable or JSON output.
    if wanted:
        hp_cols = wanted
    else:
        seen = []
        for record in records:
            for key in record["hparams"]:
                if key not in seen and _is_number(record["hparams"][key]):
                    seen.append(key)
        hp_cols = seen[:6]

    scored = [record for record in records if record["metric"] is not None]
    correlations = []
    for key in hp_cols:
        pairs = [(_floaty(record["hparams"][key]), record["metric"]) for record in scored
                 if key in record["hparams"] and _is_number(record["hparams"][key])]
        if len(pairs) >= 3:
            xs, ys = zip(*pairs)
            value = _pearson(list(xs), list(ys))
            if value is not None:
                if args.lower_is_better:
                    direction = "lower hparam -> better" if value > 0 else "higher hparam -> better"
                else:
                    direction = "higher hparam -> better" if value > 0 else "lower hparam -> better"
                correlations.append({
                    "hparam": key,
                    "pearson_r": value,
                    "direction": direction,
                    "notable": abs(value) > 0.5,
                    "sample_count": len(pairs),
                })
    best = ((min if args.lower_is_better else max)(scored, key=lambda record: record["metric"])
            if scored else None)

    if args.json:
        print(json.dumps({
            "metric_key": args.metric_key,
            "lower_is_better": args.lower_is_better,
            "runs": records,
            "correlations": correlations,
            "best_run": best,
        }, indent=2, default=str))
        return 0

    print(f"\n=== HPO Analysis (metric = {args.metric_key}, "
          f"{'lower' if args.lower_is_better else 'higher'} is better) — "
          f"{len(records)} runs ===\n")
    header = f"{'run':<28} {'metric':>12} " + " ".join(f"{k:>18}" for k in hp_cols)
    print(header)
    print("-" * len(header))
    for r in records:
        mv = f"{r['metric']:.5g}" if r["metric"] is not None else "  --  "
        cells = " ".join(f"{str(r['hparams'].get(k, '?')):>18}" for k in hp_cols)
        print(f"{r['run']:<28} {mv:>12} {cells}")
        if r["subruns"]:
            sub = ", ".join(f"{k}={'%.4g' % v if v is not None else '--'}"
                            for k, v in r["subruns"].items())
            print(f"{'':<28} subruns: {sub}")

    print("\n=== Per-hyperparameter correlation with the metric ===")
    print("(Pearson r; needs >=3 runs with both the numeric hparam and a metric. "
          "sign = direction, |r|>0.5 = notable.)\n")
    for correlation in correlations:
        flag = "  <-- notable" if correlation["notable"] else ""
        print(f"  {correlation['hparam']:<20} r={correlation['pearson_r']:+.3f}  "
              f"({correlation['direction']}){flag}  [n={correlation['sample_count']}]")
    if not correlations:
        print("  (not enough scored runs yet — need >=3 with numeric hparams + a metric)")

    if best is not None:
        print(f"\n=== Best so far: {best['run']} @ {args.metric_key}={best['metric']:.5g} ===")
        print(f"  hparams: {json.dumps(best['hparams'], default=str)}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
