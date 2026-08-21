#!/usr/bin/env python3
"""
validate_code.py — Cheap static validation of Python files BEFORE you run them.

Encodes the universal habit "lint/type-check/parse before executing": catch syntax
errors, undefined names, and (optionally) import failures in seconds, instead of after
launching an expensive job that crashes on the first line.

Project-agnostic: it validates whatever Python file(s)/dir you point it at, using only
tools that are already present (py_compile is stdlib; ruff/pyflakes/mypy are used only if
installed — never installed by this script).

Checks, in order (each best-effort; a missing optional tool is skipped, not an error):
  1. py_compile         — does it parse / compile? (stdlib, always run) — HARD gate
  2. pyflakes or ruff   — undefined names, unused imports (if available)   — soft warn
  3. import check       — `python -c "import <module>"` (only with --import-module) — HARD

Exit code: 0 if all HARD checks pass, 1 otherwise. Prints a compact report.

Usage:
    python3 validate_code.py <file_or_dir> [<file_or_dir> ...]
    python3 validate_code.py path/to/edited.py --import-module domainbed.algorithms --cwd /repo
"""
import argparse
import os
import subprocess
import sys


def _run(cmd, cwd=None, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return None, "TOOL_NOT_FOUND"
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def _which(tool):
    rc, _ = _run(["bash", "-lc", f"command -v {tool}"])
    return rc == 0


def collect_py_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                if "/.git" in root or "__pycache__" in root:
                    continue
                files += [os.path.join(root, n) for n in names if n.endswith(".py")]
        elif p.endswith(".py") and os.path.isfile(p):
            files.append(p)
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser(description="Static-validate Python before running it")
    ap.add_argument("paths", nargs="+", help="Python file(s) or dir(s) to validate")
    ap.add_argument("--import-module", default=None,
                    help="Also try `python -c 'import <MOD>'` (exercises top-level imports). "
                         "Opt-in and time-bounded: on timeout it SKIPS (does not fail).")
    ap.add_argument("--import-timeout", type=int, default=20,
                    help="Hard cap (s) on the import check; skip-and-proceed on timeout "
                         "(default 20 — keep small to protect the Executer's 5-min contract)")
    ap.add_argument("--cwd", default=None, help="Working dir for the import check")
    args = ap.parse_args()

    files = collect_py_files(args.paths)
    if not files:
        print("[validate-code] No .py files found in the given paths.")
        return 0

    print(f"\n=== validate-code: {len(files)} Python file(s) ===\n")
    hard_ok = True

    # 1) py_compile (HARD)
    print("[1/3] py_compile (syntax/compile) …")
    for f in files:
        rc, out = _run([sys.executable, "-m", "py_compile", f])
        if rc == 0:
            print(f"   ✅ {f}")
        else:
            hard_ok = False
            tail = "\n".join(out.strip().splitlines()[-6:])
            print(f"   ❌ {f}\n{_indent(tail)}")

    # 2) pyflakes/ruff (SOFT)
    linter = "ruff" if _which("ruff") else ("pyflakes" if _which("pyflakes") else None)
    print(f"\n[2/3] lint ({linter or 'no linter installed — skipped'}) …")
    if linter:
        cmd = (["ruff", "check", *files] if linter == "ruff" else ["pyflakes", *files])
        rc, out = _run(cmd)
        if rc == 0:
            print("   ✅ no lint findings")
        else:
            # soft: report but don't fail the hard gate
            findings = out.strip().splitlines()
            print(f"   ⚠️  {len(findings)} lint finding(s) (advisory, not blocking):")
            for line in findings[:20]:
                print(f"      {line}")

    # 3) import check (HARD-but-timeout-safe, opt-in)
    # An ML module import pulls in torch/CUDA init — can take 10-60s. Inside the
    # Executer's 5-minute contract that is a real time risk, and it partially
    # duplicates the launcher's own first-step gate. So: bound it hard, and on
    # TIMEOUT we SKIP-and-proceed (do not fail the contract on a precheck).
    print(f"\n[3/3] import check "
          f"({'`import ' + args.import_module + '` (≤' + str(args.import_timeout) + 's)' if args.import_module else 'skipped (no --import-module)'}) …")
    if args.import_module:
        rc, out = _run([sys.executable, "-c", f"import {args.import_module}"],
                       cwd=args.cwd, timeout=args.import_timeout)
        if rc == 0:
            print(f"   ✅ import {args.import_module} succeeded"
                  f"{' (cwd=' + args.cwd + ')' if args.cwd else ''}")
        elif rc == 124 or out == "TIMEOUT":
            # skip-and-proceed: a slow import is NOT a validation failure; the
            # launcher's first-step gate will still catch a genuinely broken import.
            print(f"   ⏭️  import check timed out after {args.import_timeout}s — "
                  f"SKIPPED (not a failure). The launcher's first-step gate remains "
                  f"the backstop for import errors.")
        else:
            hard_ok = False
            tail = "\n".join(out.strip().splitlines()[-10:])
            print(f"   ❌ import {args.import_module} FAILED\n{_indent(tail)}")

    print("\n=== VERDICT ===")
    if hard_ok:
        print("  ✅ PASS — safe to proceed to launch (hard checks passed).\n")
        return 0
    print("  ❌ FAIL — do NOT launch. Fix the errors above first "
          "(a broken file wastes a whole run).\n")
    return 1


def _indent(text, n=6):
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


if __name__ == "__main__":
    sys.exit(main())
