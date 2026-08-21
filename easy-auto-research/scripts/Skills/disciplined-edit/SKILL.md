---
name: disciplined-edit
description: "Enforce the small-safe-documented-change habit: snapshot a file (OUT-OF-TREE) before editing, review the minimal diff after (flagging scope creep) with the change record printed to stdout, revert cleanly if wrong. Use as an Executer around every code/config edit. EXECUTER skill."
allowed-tools: Bash, Read
---

# disciplined-edit — One small, reversible, documented change (Executer)

Encodes three universal habits: **make one small change at a time**, **keep every change
reversible** (within your turn), and **document what & why**. It wraps your edit with an
out-of-tree before-snapshot, an after-diff (with a scope-creep check), an easy revert, and
a change-record template.

> **Why "out-of-tree"?** The version tree already gives you *cross-cycle* rollback for free
> (each version is an immutable copy; the Planner just branches a new one from a known-good
> version). This skill only adds *intra-turn* undo — a net for when you make several edits in
> one Executer turn. Snapshots therefore go to a scratch dir **outside** the version
> directory: anything written *inside* a version dir gets copied into every child version, so
> in-tree snapshots would propagate as clutter. `review` likewise prints to **stdout only**.

## Workflow (around every edit)

Run these commands from the release repository root:

```bash
S="easy-auto-research/scripts/Skills/disciplined-edit/scripts/disciplined_edit.py"

# 1) BEFORE editing — snapshot (saved to /tmp scratch, NOT the version dir)
python3 "$S" snapshot domainbed/algorithms.py

# 2) ... make your edit with the normal Edit tool ...

# 3) AFTER editing — review the minimal diff + change record (all to stdout)
python3 "$S" review domainbed/algorithms.py

# 4) If the edit was wrong — revert precisely (intra-turn undo)
python3 "$S" revert domainbed/algorithms.py
```

## What each step gives you
- **snapshot** — backs the file up to an **out-of-tree** scratch dir
  (`$EASY_AUTO_RESEARCH_EDIT_SCRATCH` / `$TMPDIR` / `/tmp`, override with `--scratch`), keyed by the
  file's absolute path so same-named files don't collide. Nothing is written into the tree.
- **review** — prints the unified diff per file + a size summary to **stdout**, warns on
  scope creep (too many files/lines = probably more than one logical change), and ends with
  a `CHANGE RECORD` stub (WHAT / WHY / HOW-TO-REVERT / VALIDATED) to paste into your report.
- **revert** — restores the file from its out-of-tree snapshot (undo a bad edit this turn).

## How to use it
- Snapshot **before** touching a file; review **after**. Aim for a small, single-purpose
  diff — if `review` warns about sprawl, split the change or confirm it's really one thing.
- Paste the filled `CHANGE RECORD` into your `## Actions Taken` / `## Files Modified`.
- Pairs with `validate-before-run` (run it on the reviewed file before launching).

## Hard rules
- **Never writes inside a version directory** — snapshots go out-of-tree, records go to
  stdout. This is deliberate: in-tree writes propagate into every child version.
- Cross-cycle rollback is the version tree's job; this skill only covers *intra-turn* undo.
- This skill does not judge correctness; it enforces *discipline* (small, reversible,
  documented). Correctness is validated separately and judged downstream.
- Pure stdlib, never installs anything.
