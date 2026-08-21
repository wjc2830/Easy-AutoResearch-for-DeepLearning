---
name: easy-auto-research
description: "Set up, steer, and resume Easy-Auto-Research for Deep Learning — a human-supervised, multi-role deep-learning research loop for Claude Code. It proposes experiments, edits versioned code copies, launches training, monitors progress, evaluates evidence against a goal, and persists state across cycles. Use this skill through natural-language requests to start a run, provide guidance, interrupt active work, inspect progress, or continue a stopped run."
---

# Easy-Auto-Research for Deep Learning — Research Loop

Easy-Auto-Research for Deep Learning provides a human-supervised research loop in Claude Code. Six role-specialized Claude Code sessions run in a fixed cycle — **Planner → Executer → Secretary →
Verifier → Evaluator**, with a stateless **Orchestrator** routing between them — each using a
role-specific prompt. It improves the user's **own local ML
codebase** against a goal they define and versions every experiment. It may stop on verified
success, user action, abort, configured cycle/stall limits, or an unrecoverable failure.

This skill is self-contained. Everything lives under this skill's own directory. The skill
agent resolves **`$SKILL`** internally to the absolute directory containing this SKILL.md;
this path and all commands are implementation details, not part of the human interface:
- `scripts/` — the runtime controllers and `scripts/Skills/` helper skills used by the research roles.
- `templates/` — six runtime role templates, `interviewer.md`, `group_duty.md`, and `research_spec.template.json`.
- `reference/research_spec.filled_example.json` — an illustrative filled spec to adapt.
- `VERSION` — the release version.

---

## Mental model you must hold (before doing anything)

**Three layers, state flows downward only:**
1. **`$SKILL`** — read-only source bundle (this skill). Never mutated by a run.
2. **`$PROJ`** — one folder **per research task**. Gets a *copy* of the runtime and owns
   project state (`goal.md`, `agents/`, ledgers, sessions). Disposable and independent.
3. **`$RUN_ROOT/WorkSpace/` and `$RUN_ROOT/CycleReport/`** — siblings of `$PROJ`; hold
   versioned experiments (including per-version logs) and human reports.

The reason each task gets its own copy: **`harness.py` and `init.py` resolve every path
relative to their own file location.** `harness.py` treats *its own directory* as the project
root. So a `$PROJ` folder is fully self-contained and two tasks never collide — which is
exactly what makes runs portable across different Claude Code sessions.

**Two invariants — never work around them:**
- **Only the Evaluator may emit `EVAL_VERDICT: GOAL_MET`**, and the harness rejects it unless
  the cited evidence files exist on disk. No other Claude Code role (not even the Orchestrator) can end
  the run as a success.
- **`goal.md` is the single source of truth.** Never silently relax the goal to make a run
  "succeed."

**Safety gate:** `harness.py` runs **real training jobs** — launches background processes,
can **saturate GPUs**, edits code inside version copies, runs for **hours to days**. Setup and
history-reading are safe; **before launching or resuming the loop, state this plainly and get
explicit user confirmation.**

## User-facing interaction contract

The human interface is natural language only. Ask for goals, paths, constraints, approval,
guidance, or a request to continue in ordinary prose. Never instruct the user to run, copy,
watch, resume, or paste a shell command; never expose internal CLI syntax as a user procedure.
The skill agent performs every filesystem and process operation itself, then reports outcomes
in natural language. Commands may appear below only inside blocks explicitly labeled
**INTERNAL IMPLEMENTATION — SKILL AGENT ONLY** and must never be quoted to the user as steps.

---

## The six Claude Code roles (to explain the system if asked)

| Role | Does | Never does |
|-------|------|-----------|
| **Planner** | Reads log + prior versions + `goal.md` + `human_comments.txt`; proposes ONE next experiment (version + step budget). | Edits code, runs training. |
| **Executer** | Edits the harness-created current version, launches training in background, confirms step 1, exits (<5 min). | Creates version dirs, monitors past step 1, judges results. |
| **Secretary** | Writes the human-readable cycle report; supports interactive chat; forwards feedback to `human_comments.txt`. | Edits code, kills processes, judges goals. |
| **Verifier** | Polls training ~30s, kills doomed runs, emits `STATUS:` (`DONE_NORMAL`/`CRASHED`/`KILLED_EARLY_STOP`/…). | Claims the goal is met. |
| **Evaluator** | Re-reads `goal.md`, checks evidence on disk, emits `EVAL_VERDICT`. | Monitors training, edits code. |
| **Orchestrator** | Stateless router: one JSON routing decision per turn. | Reads files, runs commands. |

---

# The three tasks this skill handles

Figure out which one the user wants, then follow that section.

- **No `$PROJ` yet / "start a new research"** → **Task 1**.
- **A loop is running and the user gives an instruction** ("tell it to try X", "stop touching
  the data pipeline", "pause and explain") → **Task 2**.
- **A run exists but the harness process is not running** ("resume", "continue", "pick it back
  up") → **Task 3**.

---

## Task 1 — START a new research run

Goal: stand up a fresh `$PROJ`, generate its goal and Claude Code role prompts, and launch the loop.

### 1.1 Gather the research spec
The whole run is defined by one file: `research_spec.json`. Open
`$SKILL/templates/research_spec.template.json` (every field + hints) and
`$SKILL/reference/research_spec.filled_example.json` (an illustrative example). Collect from the
user — ask with AskUserQuestion if anything is unclear — at minimum the **REQUIRED** fields:
- `codebase_path` — absolute path to their existing ML codebase (must exist).
- `what` — the concrete task a Claude Code role can act on; **include the exact training launch command**.
- `dos` / `donts` — allowed vs forbidden actions.

Strongly recommended (else the framework delegates a guess): `how` (metric + direction),
`baseline` (current number), `terminate` (the success bar), `search_posture`
(`hparam` | `structural` | `mixed`), and optionally `method_priors` / `signal_horizon_hint`.
**Preserve the user's exact numbers, metric names, and paths — never invent them.**

### 1.2 Prepare and initialize the project
Agree with the user on an **empty** project folder and the model choice. The skill agent then
creates the self-contained run, writes the filled spec, initializes the role prompts, verifies
that all six prompts are non-empty, and reviews `goal.md` with the user. This review is the
cheapest point to correct the objective.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Execute this block yourself. Never show it
> as instructions for the user or ask the user to copy any part of it.
>
> ```bash
> mkdir -p "$PROJ"
> cp "$SKILL/scripts/init.py" "$SKILL/scripts/harness.py" "$PROJ/"
> cp -r "$SKILL/templates" "$PROJ/templates"
> cp -r "$SKILL/scripts/Skills" "$PROJ/Skills"
> cd "$PROJ"
> python3 init.py --output-dir "$PROJ" --spec "$PROJ/research_spec.json" \
>         --model <model>
> ```
>
> Before execution, write the filled spec to `$PROJ/research_spec.json`, confirm `claude` is
> available on `PATH`, and select a capable model. Afterward, verify
> `agents/{orchestrator,planner,executer,secretary,verifier,evaluator}.md`, `goal.md`,
> `PROJECT_BRIEF.md`, and `.ar_model`.

### 1.3 Launch the loop — confirm the safety gate first
Ask in natural language for explicit approval of long-running training and GPU use. Launch only
after approval.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Execute and track this process yourself.
> Never tell the user to run or watch it.
>
> ```bash
> cd "$PROJ"
> python3 harness.py --goal goal.md
> ```
>
> Keep the long-running operation attached or tracked by Claude Code. The harness loads or
> creates persistent role sessions, creates `WorkSpace/V1*` from the codebase, and runs until
> `GOAL_MET`, abort, stall-guard, or interruption.

Answer later progress requests by inspecting durable logs and ledgers. Never ask the user to
invoke runtime scripts or resume internal role sessions manually.

---

## Task 2 — HUMAN-IN-THE-LOOP (steer a running loop)

When the user gives a natural-language instruction about a run that is currently going, your
job is to **interpret intent and translate it into one of the loop's two file channels**. Both
files live in `$PROJ` (next to `harness.py`). You do NOT stop the harness for either.

**First: understand the instruction and classify its urgency.**

### Channel A — Async guidance → `$PROJ/human_comments.txt`
Use when the instruction is **direction for the next experiment** and can wait for the current
cycle to finish (e.g. "try a smaller learning rate", "explore VREx next", "stop tuning
irm_lambda", "don't touch the data pipeline"). This is the normal case.
- The **next Planner turn** exclusively claims the current unacknowledged byte prefix. Success advances a sidecar cursor; failure leaves those bytes ahead of later appends. The journal itself is never rewritten, and **only the Planner** sees pending guidance.
- The skill agent appends under an exclusive advisory lock; it never overwrites the journal.
- The user-facing interface remains a natural-language request; the queue write is internal.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Translate the user's request, then perform
> this append yourself. Never present the command to the user.
>
> ```bash
> flock "$PROJ/human_comments.txt" sh -c 'cat >> "$1"' sh "$PROJ/human_comments.txt" <<'EOF'
> <the user's directive, rephrased clearly and specifically for the Planner>
> EOF
> ```

### Channel B — Immediate interrupt → `$PROJ/human_interrupt.txt`
Use when the instruction must reach the **currently running Claude Code role right now** (e.g. "the
current run is misconfigured — fix it before continuing", "explain the current failure before
going on"). The harness polls this file during every Claude Code role call; when it sees the end marker it
kills the in-flight subprocess, exclusively claims that message, and resumes that role's
persistent session with it. The resumed delivery cannot claim its own active message. Success advances the
sidecar cursor; failure keeps its exact byte range ahead of messages appended later. The hidden
`.human_*.txt.queue.lock` and `.human_*.txt.queue.state` files are harness-owned and must not be edited.
- The skill agent must terminate the internal message with the literal marker `**end**`; an
  unmarked message is ignored. Never overwrite or rename this file; complete messages are
  acknowledged in byte order.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Translate the user's request, append it with
> the required marker, and do not expose this command or marker protocol to the user.
>
> ```bash
> flock "$PROJ/human_interrupt.txt" sh -c 'cat >> "$1"' sh "$PROJ/human_interrupt.txt" <<'EOF'
> <the user's message to the Claude Code role that is running right now>
> **end**
> EOF
> ```

**Choosing the channel:** default to **A** (async) for strategy/direction; use **B**
(interrupt) only when waiting for the cycle boundary would waste a bad run or the user
explicitly wants to break in now. If unsure which the user means, ask with AskUserQuestion.
After writing, tell the user what you wrote, to which file, and when it will take effect (A:
next Planner turn; B: as soon as the running Claude Code role's current step yields).

---

## Task 3 — CONTINUE a stopped research run

An Easy-Auto-Research for Deep Learning run is **inherently resumable** — cross-cycle memory is on disk, not in any
Claude Code role session's context. Continuing = re-reading history, then relaunching the *same* `$PROJ`. Do NOT
re-run `init.py` (that's only for first-time setup) and do NOT create a new project folder.

### 3.1 Locate the project and read its history
Confirm `$PROJ` (the folder containing `harness.py` + `goal.md`). Then brief yourself and the
user from the durable artifacts:
- `$RUN_ROOT/WorkSpace/V*/research_log.md` — per-cycle summaries stored in the active version;
  the harness resumes the counter from the latest version's log.
- `$RUN_ROOT/WorkSpace/V*/` — every experiment version and its `train_output_*/` artifacts
  (`results.jsonl`, `out.txt`, `done`). Here `$RUN_ROOT` is the parent of `$PROJ`.
- `$PROJ/.knowledge_digest.md`, `$PROJ/.plan_ledger.jsonl`, `$PROJ/.metric_ledger.jsonl` — tried
  methods, closed axes, and PRIMARY_METRIC history.
- `$RUN_ROOT/CycleReport/` — the Secretary's human reports.
- **Why did it stop?** Check the latest version's `research_log.md` and harness output: `GOAL_MET`
  (done — nothing to continue), stall-guard (N idle cycles — needs direction), a crash, or a
  manual `Ctrl+C`. Summarize this for the user.

### 3.2 (If it stalled) give it direction before relaunching
If it stopped on the stall-guard, relaunching without change will just stall again. Offer to
write a fresh directive into `human_comments.txt` (Task 2, Channel A) — e.g. a new method to
try from `goal.md`'s `## Research Strategy`, or a constraint. Incorporate whatever the user
says here.

### 3.3 Relaunch the same project — confirm the safety gate first
Ask in natural language for explicit approval of long-running training and GPU use. Resume only
after approval.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Relaunch and track the process yourself.
> Never give this command or its options to the user.
>
> ```bash
> cd "$PROJ"
> python3 harness.py --goal goal.md
> ```
>
> The model falls back to `.ar_model`. If the user's natural-language request changes cycle
> limits, pause, timeouts, replans, or another run control, translate that request into the
> corresponding `harness.py` option internally; defaults come from `RunConfig`.

On relaunch the harness automatically: resumes the **cycle counter** from `research_log.md`,
picks up the **latest version dir**, resumes each worker's persistent session from
`agents/.sessions.json` (multi-turn context preserved), seeds the current result-signature
baseline from files already on disk, and seeds the no-improvement streak/best metric from
`.metric_ledger.jsonl`. The idle `stall_streak` always starts at zero on process launch; prior
idle cycles are not reconstructed. Keep all user interaction skill-first: report status from
these durable artifacts and accept steering through natural-language requests.

---

## Guard rails the harness enforces (mention if the user asks why it stopped/searched)

- **Stall-guard** (`MAX_STALL_CYCLES`, default 5): after N consecutive idle cycles (no new
  training output) it escalates — auto-writes a method-pivot directive, then a mandatory
  arxiv-verified-search directive into `human_comments.txt` — and finally stops the run.
- **No-improvement guard** (`MAX_NOIMPROVE_CYCLES`, default 3): after N consecutive completed
  cycles whose Evaluator-declared `PRIMARY_METRIC` fails to beat the best so far, it injects a
  mandatory arxiv-verified-search directive. It **never stops** the run; it pushes for
  external help and re-arms until a new best lands.
- **Plan-dedup gate** (`MAX_DEDUP_REPROMPTS`, default 2): rejects a `refine`-tier plan that
  re-samples an already-exhausted axis; `transplant`/`novel` tiers always pass.

Users request different limits in natural language. The skill agent applies them internally
from the controls defined by `RunConfig`; it does not expose command syntax.

## Resetting a project to re-run

Accept reset requests in natural language and explain whether the user wants to return to the
pre-initialization or post-initialization state before making changes.

> **INTERNAL IMPLEMENTATION — SKILL AGENT ONLY.** Use the bundled procedure at
> `$SKILL/scripts/Skills/reset-project/SKILL.md` (also copied into each `$PROJ/Skills/`).
> **pre-init** returns to before initialization; **post-init** keeps `goal.md` and `agents/`
> while dropping run artifacts. Never delete source (`*.py`, `templates/`, `Skills/`, or
> `research_spec.json`). Do not expose the procedure's commands to the user.
