---
name: reset-project
description: "Preview-first cleanup of generated Easy-Auto-Research project state. Supports pre-init (remove init outputs and run state) and post-init (retain init outputs, remove run state). Destructive actions require explicit confirmation."
---

# reset-project — Return a project to a known state

Use this procedure only for a project directory containing `harness.py`, `init.py`, and
`templates/`. Classify every target, preview exact paths, obtain explicit confirmation, and only
then delete or truncate anything.

## Path classes

**SOURCE / RETAINED CONFIG — never delete**

`harness.py`, `init.py`, `templates/`, `Skills/`, `README.md`,
`research_spec.json`, `.ar_model`, `uuid_ledger.jsonl`, `.git/`, `.gitignore`, and the
`agents/` directory itself. The append-only UUID ledger must survive every reset.

**INIT-GENERATED**

`goal.md`, `PROJECT_BRIEF.md`, `PREFLIGHT.md`, and the six `agents/*.md` role prompts.

**RUN-ARTIFACTS**

- sibling directories `<RUN_ROOT>/WorkSpace/` and `<RUN_ROOT>/CycleReport/`, where `RUN_ROOT` is
  the parent of the project directory;
- project-local `agent_interactions/`, `agent_history/`, `logs/`, `__pycache__/`,
  `agent_thoughts.log`, `research_log.md`, and `.last_phenomena.md`;
- project-local `.knowledge_digest.md`, `.plan_ledger.jsonl`, and `.metric_ledger.jsonl`; and
- `agents/.sessions.json`.

`agents/.sessions.json` is harness-created run state, not an init output. **Delete it in post-init
mode** so the next harness launch creates fresh worker sessions.

`human_comments.txt` and `human_interrupt.txt` are append-only queue journals: retain and empty
existing files after approval, treating an absent queue as already empty, then remove their
`.queue.lock` and `.queue.state` sidecars so the next run starts at cursor zero. Cleanup refuses
symlinks, hard links, and special files, so an unsafe queue or sidecar path aborts the reset instead
of touching an external target.

## Reset modes

- **pre-init:** remove INIT-GENERATED and RUN-ARTIFACTS. Retain source/config and empty queue files.
  The next action is `python3 init.py ...`.
- **post-init:** remove only RUN-ARTIFACTS, including `agents/.sessions.json`, and empty queue files.
  Retain `goal.md`, `PROJECT_BRIEF.md`, optional `PREFLIGHT.md`, and all six role prompts. The next
  action is `python3 harness.py ...`, which starts at cycle 1 with fresh sessions.

## Safe procedure

### 1. Validate and preview; do not delete

Set an absolute project path, then run only this inspection block. The process probe is scoped to
this project's own working directories and excludes the inspecting shell itself. It reports
any other process rooted there, regardless of command name, and ignores unrelated system-wide jobs:

```bash
PROJECT_DIR="$(realpath -- /absolute/path/to/project)"
RUN_ROOT="$(dirname -- "$PROJECT_DIR")"
test -f "$PROJECT_DIR/harness.py" \
  && test -f "$PROJECT_DIR/init.py" \
  && test -d "$PROJECT_DIR/templates" \
  || { printf '%s\n' 'Not an Easy-Auto-Research project; stopping.' >&2; exit 1; }

project_processes() {
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$$" ] && [ "$pid" != "${BASHPID:-$$}" ] && [ "$pid" != "$PPID" ] || continue
    cwd="$(readlink -f -- "$proc/cwd" 2>/dev/null)" || continue
    case "$cwd/" in
      "$PROJECT_DIR/"*|"$RUN_ROOT/WorkSpace/"*) ;;
      *) continue ;;
    esac
    command="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null)" || continue
    printf '%s %s\n' "$pid" "$command"
  done
}

printf 'Project: %s\nRun root: %s\n' "$PROJECT_DIR" "$RUN_ROOT"
printf '%s\n' 'Running project processes to review:'
project_processes
printf '%s\n' 'Project entries:'
ls -la -- "$PROJECT_DIR"
printf '%s\n' 'Sibling run directories:'
ls -ld -- "$RUN_ROOT/WorkSpace" "$RUN_ROOT/CycleReport" 2>/dev/null || true
```

If any other process is rooted in the project or its workspace, stop and ask the user how to
handle it. Do not reset under a live run.

Build and show the exact delete list for the chosen mode. The common post-init list is:

```text
<RUN_ROOT>/WorkSpace
<RUN_ROOT>/CycleReport
<PROJECT_DIR>/agent_interactions
<PROJECT_DIR>/agent_history
<PROJECT_DIR>/logs
<PROJECT_DIR>/__pycache__
<PROJECT_DIR>/agent_thoughts.log
<PROJECT_DIR>/research_log.md
<PROJECT_DIR>/.last_phenomena.md
<PROJECT_DIR>/.knowledge_digest.md
<PROJECT_DIR>/.plan_ledger.jsonl
<PROJECT_DIR>/.metric_ledger.jsonl
<PROJECT_DIR>/agents/.sessions.json
```

For pre-init, additionally list `goal.md`, `PROJECT_BRIEF.md`, `PREFLIGHT.md`, and the six exact
role-prompt paths under `agents/`. Separately state that both `human_*.txt` queues will be emptied.
Wait for explicit approval of that preview.

### 2. Delete only after approval

Revalidate the project and use exact quoted paths. Do not use `rm` globs. Because approval takes
time, this block also re-runs the process probe and re-checks the queue paths, aborting before any
deletion if a run started or a queue file was replaced with a link.

```bash
PROJECT_DIR="$(realpath -- /absolute/path/to/project)"
RUN_ROOT="$(dirname -- "$PROJECT_DIR")"
test -f "$PROJECT_DIR/harness.py" \
  && test -f "$PROJECT_DIR/init.py" \
  && test -d "$PROJECT_DIR/templates" \
  || exit 1

project_processes() {
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$$" ] && [ "$pid" != "${BASHPID:-$$}" ] && [ "$pid" != "$PPID" ] || continue
    cwd="$(readlink -f -- "$proc/cwd" 2>/dev/null)" || continue
    case "$cwd/" in
      "$PROJECT_DIR/"*|"$RUN_ROOT/WorkSpace/"*) ;;
      *) continue ;;
    esac
    command="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null)" || continue
    printf '%s %s\n' "$pid" "$command"
  done
}
LIVE_PROCESSES="$(project_processes)"
if [ -n "$LIVE_PROCESSES" ]; then
  printf '%s\n%s\n' 'A process is rooted in the project or workspace; reset aborted:' "$LIVE_PROCESSES" >&2
  exit 1
fi
python3 - \
  "$PROJECT_DIR/human_comments.txt" "$PROJECT_DIR/human_interrupt.txt" \
  "$PROJECT_DIR/.human_comments.txt.queue.lock" "$PROJECT_DIR/.human_comments.txt.queue.state" \
  "$PROJECT_DIR/.human_interrupt.txt.queue.lock" "$PROJECT_DIR/.human_interrupt.txt.queue.state" <<'PY' || exit 1
import os
import stat
import sys

for path in sys.argv[1:]:
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        # An absent queue or sidecar is already in the desired empty state.
        continue
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise SystemExit(f"Refusing unsafe queue path: {path}")
PY

rm -rf -- \
  "$RUN_ROOT/WorkSpace" \
  "$RUN_ROOT/CycleReport" \
  "$PROJECT_DIR/agent_interactions" \
  "$PROJECT_DIR/agent_history" \
  "$PROJECT_DIR/logs" \
  "$PROJECT_DIR/__pycache__"
rm -f -- \
  "$PROJECT_DIR/agent_thoughts.log" \
  "$PROJECT_DIR/research_log.md" \
  "$PROJECT_DIR/.last_phenomena.md" \
  "$PROJECT_DIR/.knowledge_digest.md" \
  "$PROJECT_DIR/.plan_ledger.jsonl" \
  "$PROJECT_DIR/.metric_ledger.jsonl" \
  "$PROJECT_DIR/agents/.sessions.json"
python3 - "$PROJECT_DIR/human_comments.txt" "$PROJECT_DIR/human_interrupt.txt" <<'PY'
import os
import stat
import sys

for path in sys.argv[1:]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        # An absent queue journal is already empty; nothing to truncate.
        continue
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SystemExit(f"Refusing unsafe queue path: {path}")
    flags = os.O_WRONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SystemExit(f"Queue path changed during reset: {path}")
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)
PY
rm -f -- \
  "$PROJECT_DIR/.human_comments.txt.queue.lock" \
  "$PROJECT_DIR/.human_comments.txt.queue.state" \
  "$PROJECT_DIR/.human_interrupt.txt.queue.lock" \
  "$PROJECT_DIR/.human_interrupt.txt.queue.state"
```

For **pre-init only**, after approval also remove the exact init outputs:

```bash
rm -f -- \
  "$PROJECT_DIR/goal.md" \
  "$PROJECT_DIR/PROJECT_BRIEF.md" \
  "$PROJECT_DIR/PREFLIGHT.md" \
  "$PROJECT_DIR/agents/orchestrator.md" \
  "$PROJECT_DIR/agents/planner.md" \
  "$PROJECT_DIR/agents/executer.md" \
  "$PROJECT_DIR/agents/secretary.md" \
  "$PROJECT_DIR/agents/verifier.md" \
  "$PROJECT_DIR/agents/evaluator.md"
```

### 3. Verify

```bash
ls -la -- "$PROJECT_DIR"
test ! -e "$RUN_ROOT/WorkSpace" && test ! -e "$RUN_ROOT/CycleReport"
test ! -e "$PROJECT_DIR/agents/.sessions.json"
test ! -s "$PROJECT_DIR/human_comments.txt"
test ! -s "$PROJECT_DIR/human_interrupt.txt"
```

For post-init, also verify `goal.md`, `PROJECT_BRIEF.md`, and all six role prompts remain. For
pre-init, verify those init outputs are absent. Report the observed state; do not claim success if
any check fails.
