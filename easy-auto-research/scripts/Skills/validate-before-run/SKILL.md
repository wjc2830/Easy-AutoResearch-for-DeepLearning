---
name: validate-before-run
description: "Cheap static validation of Python code BEFORE executing it — py_compile (syntax), optional lint (ruff/pyflakes), and an optional import check. Use as an Executer after editing any .py and before launching a job, to catch syntax/import errors in seconds instead of after wasting compute. EXECUTER skill."
allowed-tools: Bash, Read
---

# validate-before-run — Static-check code before you run it (Executer)

The universal engineering habit: **lint and parse before you execute.** A file that won't
compile or import will crash the job on its first line and waste the whole run. This skill
runs the cheapest possible checks first so you fail fast, for free.

## When to use
- Immediately **after** you edit any `.py` file and **before** you launch training or any
  long job.
- After applying a patch to a dependency/module, to confirm you didn't break its import.

## How to run

The script lives next to this SKILL.md. Run these commands from the release repository root:

```bash
# validate the file(s) you just edited (fast: syntax + lint only)
python3 easy-auto-research/scripts/Skills/validate-before-run/scripts/validate_code.py path/to/edited_file.py

# ALSO exercise the import chain — opt-in, time-bounded (skips on timeout, never blocks):
python3 easy-auto-research/scripts/Skills/validate-before-run/scripts/validate_code.py path/to/edited_file.py \
    --import-module some.entry.module --cwd /path/to/repo --import-timeout 20
```

## Checks (fast → thorough)
1. **py_compile** (stdlib, always) — does every file parse/compile? **Hard gate.** Sub-second.
2. **lint** (ruff or pyflakes, only if installed) — undefined names, unused imports. *Advisory.* Sub-second.
3. **import check** (only with `--import-module`) — `python -c "import <mod>"` from `--cwd`.
   **Time-bounded (default 20s):** on TIMEOUT it **skips-and-proceeds** (a slow torch/CUDA
   import is not a validation failure); only a genuine `ImportError` fails the gate. This
   protects the Executer's 5-minute contract — the launcher's own first-step gate remains
   the backstop for import errors.

Exit 0 = all hard checks pass (safe to launch). Exit 1 = do not launch; fix first.
(A timed-out import check does NOT set exit 1.)

## How to use the result
- **PASS** → proceed to launch the job.
- **FAIL** → do NOT launch. Report the exact error in your `## Errors Encountered` and,
  if it's within your plan's scope, fix and re-validate; otherwise report it so the next
  plan can address it. Never launch a job you already know won't import.

## Hard rules
- **Never installs anything** — optional tools (ruff/pyflakes/mypy) are used only if already
  present; their absence is skipped, not an error. Respects any "do not install packages" rule.
- Read-only w.r.t. the code (it only compiles/imports; it does not edit).
- A PASS means "it runs," not "it's correct" — correctness is still judged downstream.
