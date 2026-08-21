#!/usr/bin/env python3
"""
disciplined_edit.py — Enforce the "small, safe, documented change" habit.

Two subcommands wrap the discipline of a careful programmer:

  snapshot <file> [<file> ...]
      Save a backup copy of each file BEFORE you edit it, into an OUT-OF-TREE scratch
      dir (NOT inside the version directory). This gives *intra-turn* undo — a safety net
      for making several edits in one Executer turn — WITHOUT polluting the immutable
      version tree (anything written inside a version dir gets copytree-copied into every
      child version; snapshots must never live there). Cross-cycle rollback is already
      provided for free by the version tree itself, so this is only the fine-grained,
      within-a-turn complement.

  review <file> [<file> ...]
      After editing, show the unified diff vs the snapshot for each file, plus a
      change-size summary (lines added/removed, hunks). Flags edits that sprawl beyond
      a small, single-purpose change. Emits a ready-to-fill CHANGE RECORD block. ALL
      output goes to STDOUT only — it never writes a record file into the tree.

  revert <file> [<file> ...]
      Restore a file from its out-of-tree snapshot (undo a bad edit within the turn).

Project-agnostic; pure stdlib; never installs anything; never writes inside the version dir.

Snapshot location: <SCRATCH>/easy_auto_research_edit_snapshots/<sha of abspath>/<basename>.orig
where SCRATCH = $EASY_AUTO_RESEARCH_EDIT_SCRATCH or $TMPDIR or /tmp. Override with --scratch.

Usage:
    python3 disciplined_edit.py snapshot foo.py bar.py
    # ... make your edit ...
    python3 disciplined_edit.py review foo.py bar.py
    python3 disciplined_edit.py revert foo.py        # if the edit was wrong
"""
import argparse
import difflib
import hashlib
import os
import shutil
import sys

# a "small" change heuristic — beyond this, warn about scope creep
SPRAWL_LINES = 60
SPRAWL_FILES = 5


def _scratch_root(override=None):
    base = (override or os.environ.get("EASY_AUTO_RESEARCH_EDIT_SCRATCH")
            or os.environ.get("TMPDIR") or "/tmp")
    return os.path.join(base, "easy_auto_research_edit_snapshots")


def _snap_path(f, scratch=None):
    """Out-of-tree snapshot path, keyed by a hash of the file's absolute path so two
    different files with the same basename never collide."""
    ap = os.path.abspath(f)
    key = hashlib.sha1(os.path.dirname(ap).encode()).hexdigest()[:16]
    d = os.path.join(_scratch_root(scratch), key)
    return os.path.join(d, os.path.basename(f) + ".orig")


def cmd_snapshot(files, scratch):
    for f in files:
        if not os.path.isfile(f):
            print(f"  ⚠️  not a file, skipped: {f}")
            continue
        sp = _snap_path(f, scratch)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        shutil.copy2(f, sp)
        print(f"  📸 snapshot (out-of-tree): {f}")
    print(f"\nSnapshots saved under {_scratch_root(scratch)} (NOT in the version tree). "
          f"Make your edit, then run `review`.")
    return 0


def _read(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return None


def cmd_review(files, scratch):
    total_add = total_del = 0
    touched = 0
    print("\n=== disciplined-edit: review ===\n")
    for f in files:
        sp = _snap_path(f, scratch)
        if not os.path.isfile(sp):
            print(f"  ⚠️  no snapshot for {f} — run `snapshot` BEFORE editing next time.\n")
            continue
        old = _read(sp) or []
        new = _read(f)
        if new is None:
            print(f"  ⚠️  cannot read {f}\n")
            continue
        diff = list(difflib.unified_diff(old, new, fromfile=f"{f} (before)",
                                         tofile=f"{f} (after)"))
        add = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        rem = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        hunks = sum(1 for l in diff if l.startswith("@@"))
        if not diff:
            print(f"  • {f}: no change vs snapshot.")
            continue
        touched += 1
        total_add += add
        total_del += rem
        print(f"  • {f}: +{add} / -{rem} lines, {hunks} hunk(s)")
        for line in diff:
            print("    " + line.rstrip("\n"))
        print()

    print("=== change-size check ===")
    if touched > SPRAWL_FILES:
        print(f"  ⚠️  {touched} files changed (> {SPRAWL_FILES}). A good change is small and "
              f"single-purpose — are you doing more than one thing at once?")
    if total_add + total_del > SPRAWL_LINES:
        print(f"  ⚠️  {total_add + total_del} lines changed (> {SPRAWL_LINES}). Consider "
              f"whether this is really ONE logical change; large diffs are harder to review "
              f"and revert.")
    if touched <= SPRAWL_FILES and total_add + total_del <= SPRAWL_LINES:
        print(f"  ✅ small, focused change ({touched} file(s), "
              f"{total_add + total_del} lines).")

    print("\n=== CHANGE RECORD (paste into your report — this is stdout only, "
          "nothing is written into the version tree) ===")
    print("  WHAT changed : <one line — the exact edit, e.g. 'lr 1e-3 → 5e-4 in --hparams'>")
    print("  WHY          : <the reason / hypothesis this serves>")
    print("  HOW TO REVERT: `disciplined_edit.py revert " + " ".join(files) + "`")
    print("  VALIDATED    : <did you run validate-before-run after? result?>\n")
    return 0


def cmd_revert(files, scratch):
    for f in files:
        sp = _snap_path(f, scratch)
        if not os.path.isfile(sp):
            print(f"  ⚠️  no snapshot to revert {f}")
            continue
        shutil.copy2(sp, f)
        print(f"  ↩️  reverted {f} to snapshot")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Small, safe, documented edits (out-of-tree snapshots)")
    ap.add_argument("--scratch", default=None,
                    help="Override the out-of-tree snapshot root (default: "
                         "$EASY_AUTO_RESEARCH_EDIT_SCRATCH / $TMPDIR / /tmp)")
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("snapshot", "review", "revert"):
        s = sub.add_parser(name)
        s.add_argument("files", nargs="+")
    args = ap.parse_args()
    if args.action == "snapshot":
        return cmd_snapshot(args.files, args.scratch)
    if args.action == "review":
        return cmd_review(args.files, args.scratch)
    return cmd_revert(args.files, args.scratch)


if __name__ == "__main__":
    sys.exit(main())
