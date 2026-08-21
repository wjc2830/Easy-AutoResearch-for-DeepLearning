#!/usr/bin/env python3
"""init.py — Easy-Auto-Research for Deep Learning project initializer.

Bootstraps a project with one interviewer session plus six parallel role-writer
sessions, driven through a 5-state machine:

    S1  INSPECT_CODEBASE          — read the target codebase (path from spec)
    S2  INGEST_SPEC               — ingest the pre-filled research_spec.json
    S3  WRITE_GOAL                — synthesize goal.md + PROJECT_BRIEF.md
    S5  WRITE_AGENTS              — generate agents/<role>.md (6 files)
    S6  POLISH                    — self-review + cross-file consistency pass

Setup order (before the interviewer runs):
    Step 1  verify Claude Code is available
    Step 2  pick the Claude model          → .ar_model
    Step 3  fill research_spec.json        → the spec

The interviewer handles inspection, goal/brief synthesis, and final review.
The init.py controller performs state transitions and launches six Claude Code role writers in parallel.
The script:
  - verifies Claude Code, picks a model, and sets up the session
  - prepares + validates research_spec.json
  - parses [TAG] action lines from the agent's responses
  - services [ASK_USER] / [CONFIRM_USER] (safety net; normally unused)
  - sends the next [BEGIN_S<n>] when the agent says [STATE_DONE S<n>]
  - exits cleanly on [STATE_DONE S6] / [ABORT]

Usage:
    python3 init.py --output-dir /path/to/your/ar-project
    python3 init.py --output-dir . --model claude-opus-4-6
    python3 init.py --output-dir . --spec /path/to/filled_research_spec.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime

# ---------------------------------------------------------------------------
# ANSI colors (minimal — no animations)
# ---------------------------------------------------------------------------
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RESET = "\033[0m"

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "templates")
INTERVIEWER_TEMPLATE = os.path.join(TEMPLATES_DIR, "interviewer.md")
MODEL_CONFIG_FILENAME = ".ar_model"

# The pre-filled research spec (replaces the old interactive S2 interview).
SPEC_TEMPLATE = os.path.join(TEMPLATES_DIR, "research_spec.template.json")
SPEC_FILENAME = "research_spec.json"

# ---------------------------------------------------------------------------
# Claude Code configuration
# ---------------------------------------------------------------------------
CLAUDE_BIN = "claude"
CLAUDE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


def _mangle_claude(cwd: str) -> str:
    """Map an absolute cwd to Claude Code's project-directory name."""
    return "-" + cwd.replace("/", "-").lstrip("-")

# Append-only ledger of every session UUID this script ever mints. Lives at
# the script-dir root; shared with harness.py (same filename, same dir,
# same JSONL schema) so a single `cat uuid_ledger.jsonl` shows the full
# session history of this auto-research project (init + every harness
# cycle). Re-init.md does NOT delete this file.
UUID_LEDGER_FILENAME = "uuid_ledger.jsonl"


def _uuid_ledger_path() -> str:
    """Return the absolute path to the project's UUID ledger."""
    return os.path.join(SCRIPT_DIR, UUID_LEDGER_FILENAME)


def _claude_jsonl_path_for(session_id: str) -> str:
    """Return the expected Claude Code session JSONL path for SCRIPT_DIR."""
    mangled = _mangle_claude(SCRIPT_DIR)
    return os.path.expanduser(f"~/.claude/projects/{mangled}/{session_id}.jsonl")


def record_session_uuid(role: str, session_id: str, source: str) -> None:
    """Append one record to uuid_ledger.jsonl. Best-effort; silently no-ops
    on filesystem errors. Schema is identical to harness.py's writer
    so the two share one ledger file."""
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "role": role,
            "session_id": session_id,
            "source": source,
            "cycle": None,
            "jsonl_path": _claude_jsonl_path_for(session_id),
        }
        with open(_uuid_ledger_path(), "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass

# Hard cap on agent turns to prevent runaway sessions. Each [BEGIN_S<n>] →
# [STATE_DONE S<n>] cycle typically uses 5-30 turns; full run is ~50-100.
MAX_INTERVIEWER_TURNS = 200

# Per-call timeout. Generous because S5 (write 6 agents) is expensive.
PER_CALL_TIMEOUT = 1800  # 30 minutes

# State sequence the interviewer walks through.
STATES = [
    ("S1", "INSPECT_CODEBASE"),
    ("S2", "INGEST_SPEC"),
    ("S3", "WRITE_GOAL"),
    ("S5", "WRITE_AGENTS"),
    ("S6", "POLISH"),
]

# The six required runtime agent prompts written during S5.
AGENT_ROLES = [
    ("planner", True),
    ("executer", True),
    ("verifier", True),
    ("evaluator", True),
    ("orchestrator", True),
    ("secretary", True),
]

# Module-globals resolved during the setup phase.
SELECTED_MODEL: str | None = None  # model id, or None for Claude Code default
SKIP_AGENT_PERMISSIONS = False

_RE_MODEL_NAME = re.compile(r"^[A-Za-z0-9._:\-]+$")

# ---------------------------------------------------------------------------
# User-facing labels (English only — the interviewer handles its own language)
# ---------------------------------------------------------------------------
LABELS = {
    "model_header": "Step 2 — select the Claude model for the interviewer session",
    "model_choice_prompt": "Enter 1-{n}: ",
    "model_custom_prompt": "Enter custom model name: ",
    "model_invalid": "Invalid model name; use letters, digits, dots, dashes, underscores, colons.",
    "model_validation_failed": "Claude Code rejected --model {model}: {err}",
    "model_saved": "Model saved to {file}: {model}",
    "model_default_saved": "Using the Claude Code default model.",
    "cli_missing": "Claude Code executable 'claude' not found on PATH.",
    "templates_missing": "Interviewer template not found: {path}",
    "spec_template_missing": "Research-spec template not found: {path}",
    "agent_dead": "Interviewer call failed (no output). Aborting.",
    "max_turns": "Reached MAX_INTERVIEWER_TURNS={n}. Aborting to prevent runaway session.",
    "all_done": "Init complete. Project ready at: {path}",
}


def t(key: str, **kwargs) -> str:
    template = LABELS.get(key, key)
    return template.format(**kwargs) if kwargs else template


# ---------------------------------------------------------------------------
# Logging helpers (minimal, console-only)
# ---------------------------------------------------------------------------
def info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}", flush=True)


def status(msg: str) -> None:
    print(f"  {CYAN}{msg}{RESET}", flush=True)


def good(msg: str) -> None:
    print(f"  {GREEN}{msg}{RESET}", flush=True)


def warn(msg: str) -> None:
    print(f"  {YELLOW}{msg}{RESET}", flush=True)


def fatal(msg: str, code: int = 1) -> None:
    print(f"  {RED}{msg}{RESET}", flush=True)
    sys.exit(code)


def header(text: str) -> None:
    print()
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"  {BOLD}{text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}")


# ---------------------------------------------------------------------------
# Setup phase: Claude Code, model, prerequisites
# ---------------------------------------------------------------------------
def check_cli_available(bin_name: str) -> bool:
    """True if `bin_name --version` runs. Does not exit."""
    if shutil.which(bin_name) is None:
        return False
    try:
        r = _run_process_group(
            [bin_name, "--version"], input_text=None, timeout=15,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _write_config(output_dir: str, filename: str, value: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    try:
        with open(path, "w") as f:
            f.write(value + "\n")
    except OSError as e:
        fatal(f"Failed to write {path}: {e}")


def validate_model_with_cli(model: str) -> tuple[bool, str]:
    """Validate a model with Claude Code."""
    try:
        r = _run_process_group(
            [CLAUDE_BIN, "--model", model, "--version"],
            input_text=None, timeout=15,
        )
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        return False, (err[0] if err else f"exit code {r.returncode}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def prompt_model_selection(output_dir: str) -> None:
    """Step 2 picker: choose a Claude model and write .ar_model."""
    global SELECTED_MODEL

    detected = [m for m in CLAUDE_MODELS if validate_model_with_cli(m)[0]]
    models = [(m, "") for m in detected]
    options = list(models) + [
        ("__default__", "Use the Claude Code default model"),
        ("__custom__", "Enter a custom model name"),
    ]

    header(t("model_header"))
    for i, (name, desc) in enumerate(options, 1):
        if name.startswith("__"):
            print(f"  {i}. {DIM}({desc}){RESET}")
        else:
            label = f"{name:<30}" + (f" — {desc}" if desc else "")
            print(f"  {i}. {GREEN}{label}{RESET}")

    n = len(options)
    while True:
        try:
            raw = input(f"\n  {CYAN}{t('model_choice_prompt', n=n)}{RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        if not raw.isdigit() or not (1 <= int(raw) <= n):
            warn(f"Please enter 1-{n}.")
            continue
        name, _ = options[int(raw) - 1]

        if name == "__default__":
            SELECTED_MODEL = None
            stale = os.path.join(output_dir, MODEL_CONFIG_FILENAME)
            if os.path.isfile(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            good(t("model_default_saved"))
            return

        if name == "__custom__":
            try:
                custom = input(f"  {CYAN}{t('model_custom_prompt')}{RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not custom or not _RE_MODEL_NAME.fullmatch(custom):
                warn(t("model_invalid"))
                continue
            chosen = custom
            ok, err = validate_model_with_cli(chosen)
            if not ok:
                warn(t("model_validation_failed", model=chosen, err=err))
                continue
        else:
            chosen = name  # already detected/validated

        SELECTED_MODEL = chosen
        _write_config(output_dir, MODEL_CONFIG_FILENAME, chosen)
        good(t("model_saved", model=chosen, file=MODEL_CONFIG_FILENAME))
        return


# ---------------------------------------------------------------------------
# Claude Code invocation
# ---------------------------------------------------------------------------
def build_claude_cmd(*extra_args: str) -> list[str]:
    """Build a Claude Code command list and pin the selected model when set."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if SELECTED_MODEL:
        cmd += ["--model", SELECTED_MODEL]
    if SKIP_AGENT_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    cmd += list(extra_args)
    return cmd


PROCESS_GROUP_GRACE_S = 2


def _process_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_process_group_gone(pgid, timeout, proc=None):
    deadline = time.monotonic() + timeout
    while _process_group_exists(pgid):
        if proc is not None:
            proc.poll()  # reap the leader; descendants may still keep the group alive
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _terminate_process_group(proc):
    """TERM a process group, wait for the group, then KILL every survivor."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    if not _wait_process_group_gone(pgid, PROCESS_GROUP_GRACE_S, proc):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        _wait_process_group_gone(pgid, PROCESS_GROUP_GRACE_S, proc)
    try:
        proc.wait(timeout=0)
    except Exception:
        pass


def _run_process_group(cmd, *, input_text, timeout, cwd=None):
    """Run a command in a new session and clean its whole group on every exit path."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        try:
            proc.communicate(timeout=PROCESS_GROUP_GRACE_S)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    if _process_group_exists(proc.pid):
        _terminate_process_group(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def call_interviewer(
    session_id: str,
    message: str,
    *,
    first_call: bool,
    system_prompt_file: str,
) -> str | None:
    """Send `message` to the interviewer session. Returns stripped stdout or None on failure.

    First call uses --system-prompt-file + --session-id; subsequent calls use --resume.
    Tools (Read, Write, Edit, Glob, Grep, Bash) are enabled for the entire session
    via --dangerously-skip-permissions.
    """
    if first_call:
        cmd = build_claude_cmd(
            "--system-prompt-file", system_prompt_file,
            "--session-id", session_id,
        )
    else:
        cmd = build_claude_cmd(
            "--resume", session_id,
        )

    try:
        r = _run_process_group(
            cmd, input_text=message, timeout=PER_CALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        warn(f"Interviewer call timed out after {PER_CALL_TIMEOUT}s.")
        return None

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        warn(f"Interviewer exited code {r.returncode}: {stderr[:300]}")
        return None
    return (r.stdout or "").strip()


# ---------------------------------------------------------------------------
# Action-tag parsing
# ---------------------------------------------------------------------------
ACTION_TAG_RE = re.compile(r"^\[([A-Z_]+(?:\s+S\d+)?)\]\s*(.*)", re.DOTALL)


def parse_action(response: str) -> tuple[str, str]:
    """Return (tag, body) where tag is e.g. 'ASK_USER' or 'STATE_DONE S2'.

    The tag MUST be the first non-whitespace token of the response.
    Body is everything after the closing `]` (may span multiple lines).
    Returns ('UNKNOWN', full_response) if no recognizable tag found.
    """
    if not response:
        return "UNKNOWN", ""
    text = response.lstrip()
    m = ACTION_TAG_RE.match(text)
    if not m:
        return "UNKNOWN", text
    tag = m.group(1).strip()
    body = m.group(2).strip()
    return tag, body


def split_first_line(body: str) -> tuple[str, str]:
    """Split body into (first_line, rest). Used for tags whose body's first
    line is a key (e.g. [NOTE] <key>\\n<answer>)."""
    if not body:
        return "", ""
    parts = body.split("\n", 1)
    if len(parts) == 1:
        return parts[0].strip(), ""
    return parts[0].strip(), parts[1].strip()


# ---------------------------------------------------------------------------
# Interviewer driver — the state machine
# ---------------------------------------------------------------------------
def read_user_input() -> str:
    """Read multi-line input from the user. Blank line ends input.
    Single-line answers can also be entered with Enter."""
    print(f"  {DIM}(end with blank line for multi-line, or just press Enter for single-line){RESET}")
    print(f"  {CYAN}You> {RESET}", end="", flush=True)
    lines: list[str] = []
    blank_count = 0
    try:
        while True:
            line = input()
            if line == "":
                if not lines:
                    return ""
                blank_count += 1
                if blank_count >= 1:
                    break
            else:
                blank_count = 0
                lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()
    return "\n".join(lines).strip()


def confirm_yes_no_edit() -> tuple[str, str]:
    """For [CONFIRM_USER]: prompt user for [Y/n/edit]. Returns (verdict, body)
    where verdict is one of 'OK', 'EDIT', 'REJECT' and body is the edited text
    if verdict=='EDIT' else empty."""
    while True:
        try:
            choice = input(f"  {CYAN}Apply this? [Y/n/edit]: {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "REJECT", ""
        if choice in ("", "y", "yes"):
            return "OK", ""
        if choice in ("n", "no"):
            return "REJECT", ""
        if choice in ("e", "edit"):
            print(f"  {DIM}Enter your edited version (blank line to finish):{RESET}")
            edited = read_user_input()
            if edited:
                return "EDIT", edited
            warn("Empty edit — treating as reject.")
            return "REJECT", ""
        warn("Please enter Y, n, or edit.")


def display_show(text: str) -> None:
    """Display interviewer's [SHOW] message to the user."""
    print()
    print(f"{MAGENTA}{'─' * 60}{RESET}")
    for line in text.splitlines():
        print(f"  {line}")
    print(f"{MAGENTA}{'─' * 60}{RESET}")


def display_question(text: str) -> None:
    """Display [ASK_USER] prompt."""
    print()
    print(f"{BOLD}{GREEN}>> {text}{RESET}")


def display_confirm(key: str, text: str) -> None:
    """Display [CONFIRM_USER] polished answer."""
    print()
    print(f"{BOLD}{BLUE}Polished answer for [{key}]:{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")
    for line in text.splitlines():
        print(f"  {line}")
    print(f"{DIM}{'─' * 60}{RESET}")


def display_state_transition(state_id: str, name: str) -> None:
    print()
    print(f"{BOLD}{CYAN}━━━ Entering {state_id}: {name} ━━━{RESET}")


# ---------------------------------------------------------------------------
# Research spec (Step 3): the pre-filled JSON that replaces the old
# interactive interview. init.py copies a template, waits for the user to
# fill it, then validates the required fields before the interviewer runs.
# ---------------------------------------------------------------------------
SPEC_REQUIRED_STR = ["codebase_path", "what"]     # non-empty strings
SPEC_REQUIRED_LIST = ["dos", "donts"]              # >=1 non-empty entry
# Optional strategy fields (all have sane defaults if empty; never block setup):
#   search_posture: "hparam" | "structural" | "mixed" (default "mixed")
#   method_priors: list[str] of known techniques worth trying (default [])
#   signal_horizon_hint: str, verifier kill-early patience hint (default "")
# These flow through the interviewer into goal.md's "## Research Strategy"
# section, then into agents/*.md at S5 — no init.py code branches on them.
SPEC_OPTIONAL_STRATEGY = ["search_posture", "method_priors", "signal_horizon_hint"]


def _load_spec(path: str) -> tuple[dict | None, str]:
    """Parse a spec JSON file. Returns (data, error_message)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, f"file not found: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e})"
    except OSError as e:
        return None, f"cannot read file ({e})"
    if not isinstance(data, dict):
        return None, "top-level JSON must be an object"
    return data, ""


def validate_spec(data: dict) -> list[str]:
    """Return a list of human-readable problems. Empty list == valid."""
    problems: list[str] = []
    for key in SPEC_REQUIRED_STR:
        val = data.get(key, "")
        if not isinstance(val, str) or not val.strip():
            problems.append(f"'{key}' is required and must be a non-empty string.")
    # codebase_path must be an existing absolute directory.
    cb = data.get("codebase_path", "")
    if isinstance(cb, str) and cb.strip():
        if not os.path.isabs(cb):
            problems.append(f"'codebase_path' must be an absolute path (got: {cb}).")
        elif not os.path.isdir(cb):
            problems.append(f"'codebase_path' does not exist or is not a directory: {cb}.")
    for key in SPEC_REQUIRED_LIST:
        val = data.get(key, None)
        if not isinstance(val, list) or not any(
            isinstance(x, str) and x.strip() for x in val
        ):
            problems.append(f"'{key}' is required and must have at least one non-empty entry.")
    # Optional: if search_posture is given, it must be one of the known values.
    posture = data.get("search_posture", "")
    if isinstance(posture, str) and posture.strip():
        if posture.strip().lower() not in ("hparam", "structural", "mixed"):
            problems.append(
                f"'search_posture' must be one of 'hparam' | 'structural' | 'mixed' "
                f"(got: {posture!r}); leave empty for the default 'mixed'."
            )
    return problems


def _extract_entry_module(spec_data: dict) -> str | None:
    """Best-effort: pull an importable module path from a `python[3] -m <mod>` or
    `python[3] <path/to/script.py>` invocation mentioned anywhere in the spec's
    'what'/'how' text. Returns a dotted module (for -m) or a .py path, or None."""
    blob = " ".join(
        str(spec_data.get(k, "")) for k in ("what", "how", "baseline", "terminate")
    )
    m = re.search(r"python3?\s+-m\s+([A-Za-z_][\w.]+)", blob)
    if m:
        return m.group(1)
    m = re.search(r"python3?\s+([^\s'\"]+\.py)\b", blob)
    if m:
        return m.group(1)
    return None


def preflight_codebase_import(output_dir: str, spec_data: dict) -> None:
    """Pre-flight check: before generating goal.md/agents, actually try to import
    the codebase's training entry point so environment-level blockers (missing
    packages, broken top-level imports) surface NOW — not after N wasted cycles.

    Non-fatal by design: many codebases aren't cleanly importable out-of-process,
    and a false negative must not block setup. On failure it prints a loud warning
    and writes `PREFLIGHT.md` in output_dir so the interviewer can fold the finding
    into goal.md's constraints (e.g. authorize a lazy-import patch up front).

    Motivation: in a prior run, an unconditional `import wilds` / `import timm` in
    the codebase crashed every training launch at import time; the loop burned ~9
    cycles discovering + escalating this before any training ran. A 3-second import
    probe at setup catches exactly that class of failure.
    """
    codebase = (spec_data or {}).get("codebase_path", "").strip()
    if not codebase or not os.path.isdir(codebase):
        return  # spec validation already handles a bad path

    entry = _extract_entry_module(spec_data)
    if not entry:
        info("Pre-flight: no `python -m <module>` / script entry point found in the "
             "spec text — skipping the import probe (not an error).")
        return

    header("Pre-flight — codebase import probe")
    info(f"Codebase: {codebase}")
    info(f"Entry point detected in spec: {entry}")

    if entry.endswith(".py"):
        # import a file path: prefer a syntax/def-level check via py_compile, then
        # a real import so top-level `import <dep>` lines are exercised.
        script = entry if os.path.isabs(entry) else os.path.join(codebase, entry)
        probe = (
            f"import runpy, sys; sys.argv=['x']; "
            f"import importlib.util as u; "
            f"spec=u.spec_from_file_location('_preflight_mod', {script!r}); "
            f"m=u.module_from_spec(spec); spec.loader.exec_module(m)"
        )
    else:
        # dotted module: `import domainbed.scripts.train` exercises the whole chain
        probe = f"import importlib; importlib.import_module({entry!r})"

    try:
        result = _run_process_group(
            [sys.executable, "-c", probe], input_text=None,
            cwd=codebase, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        warn(f"Pre-flight import probe could not run ({e}) — skipping (non-fatal).")
        return

    if result.returncode == 0:
        good(f"Pre-flight OK: `{entry}` imports cleanly in {codebase}. "
             f"No environment-level import blocker detected.")
        return

    # Failure — surface it loudly and persist it for the interviewer.
    stderr = (result.stderr or "").strip()
    tail = "\n".join(stderr.splitlines()[-15:]) if stderr else "(no stderr captured)"
    missing = re.findall(r"No module named ['\"]([\w.]+)['\"]", stderr)
    warn("=" * 70)
    warn("PRE-FLIGHT IMPORT FAILURE — the training entry point does NOT import.")
    warn(f"Entry point: {entry}")
    if missing:
        warn(f"Missing module(s): {', '.join(sorted(set(missing)))}")
    warn("This WILL crash every training launch at import time, regardless of "
         "hyperparameters. Resolve it before/at cycle 1 (install the package if "
         "your constraints allow, or authorize a lazy-import guard in goal.md) so "
         "the loop does not waste cycles rediscovering it.")
    warn("Full traceback tail:")
    for line in tail.splitlines():
        warn(f"  {line}")
    warn("=" * 70)

    try:
        pf = os.path.join(output_dir, "PREFLIGHT.md")
        os.makedirs(output_dir, exist_ok=True)
        with open(pf, "w") as f:
            f.write("# Pre-flight Codebase Import — FAILURE\n\n")
            f.write(f"*Generated by init.py: {datetime.now():%Y-%m-%d %H:%M:%S}*\n\n")
            f.write(f"- **Codebase**: `{codebase}`\n")
            f.write(f"- **Entry point probed**: `{entry}`\n")
            if missing:
                f.write(f"- **Missing module(s)**: {', '.join(sorted(set(missing)))}\n")
            f.write("\nThe training entry point fails to import in the target "
                    "environment. Every training launch will crash at import time "
                    "until this is resolved. The interviewer should reflect this in "
                    "goal.md — either (a) note the required package install as an "
                    "authorized setup step, or (b) explicitly permit a narrow "
                    "lazy-import guard on the offending unconditional import so the "
                    "unused-dependency path no longer breaks module load.\n\n")
            f.write("## Traceback (tail)\n\n```\n" + tail + "\n```\n")
        info(f"Pre-flight finding written to {pf} (the interviewer will read it).")
    except OSError as e:
        warn(f"Could not write PREFLIGHT.md: {e}")


def prepare_spec(output_dir: str, spec_arg: str | None) -> tuple[str, dict]:
    """Resolve the research spec. Returns (spec_path, spec_data).

    If --spec was given, load+validate it directly (fatal on error).
    Otherwise copy the template into <output_dir>/research_spec.json, prompt
    the user to fill it, and loop (wait-for-Enter → re-validate) until valid."""
    if spec_arg:
        spec_path = os.path.abspath(spec_arg)
        data, err = _load_spec(spec_path)
        if data is None:
            fatal(f"--spec {spec_path}: {err}")
        problems = validate_spec(data)
        if problems:
            for p in problems:
                warn(f"  - {p}")
            fatal(f"--spec {spec_path} is missing required fields (see above).")
        good(f"Using research spec: {spec_path}")
        return spec_path, data

    if not os.path.isfile(SPEC_TEMPLATE):
        fatal(t("spec_template_missing", path=SPEC_TEMPLATE))

    spec_path = os.path.join(output_dir, SPEC_FILENAME)
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.isfile(spec_path):
        shutil.copyfile(SPEC_TEMPLATE, spec_path)

    header("Step 3 — fill in the research spec")
    print(f"  {BOLD}A research-spec template has been written to:{RESET}")
    print(f"    {CYAN}{spec_path}{RESET}")
    print(f"  {DIM}Open it in another window, fill in the fields "
          f"(required: codebase_path, what, dos, donts), save, then return here.{RESET}")

    while True:
        try:
            input(f"\n  {CYAN}Press Enter once you've saved the spec (Ctrl-C to abort)... {RESET}")
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        data, err = _load_spec(spec_path)
        if data is None:
            warn(f"Could not read the spec: {err}")
            continue
        problems = validate_spec(data)
        if problems:
            warn("The spec is not ready yet:")
            for p in problems:
                warn(f"  - {p}")
            continue
        good(f"Research spec validated: {spec_path}")
        return spec_path, data


# ---------------------------------------------------------------------------
# S5 (WRITE_AGENTS) — init.py-controlled, parallel Claude Code role writers
# ---------------------------------------------------------------------------
# Instead of the interviewer writing the 6 agent prompts one-by-one, init.py
# fans out one Claude Code subprocess per role. Each Claude Code role writer fills a role template
# (templates/<role>.md) using goal.md + PROJECT_BRIEF.md as context and writes
# agents/<role>.md itself. Every role writer uses Claude Code with the same selected
# model (via build_claude_cmd), so setup honours that choice.

_AGENT_WRITER_INSTRUCTIONS = """\
You are a Claude Code project-setup worker. Your ONLY job is to write ONE runtime agent
system-prompt file for an auto-research project, then stop.

Role to generate: {role}
Template (the required structure): {template_path}
Project goal:                      {goal_path}
Project brief:                     {brief_path}
Write your output to:              {out_path}

Steps (use your own Read/Write tools; do not ask questions):
1. Read {template_path} — this is the structural template with {{{{PLACEHOLDER}}}}
   markers. Read {goal_path} and {brief_path} for the project specifics.
2. Produce the project-specific system prompt for the `{role}` agent by filling
   EVERY {{{{PLACEHOLDER}}}} with concrete values drawn from goal.md and the brief.
3. MANDATORY structural fidelity: every `## ` heading in the template must appear
   in your output, in the same order, none dropped or renamed. You may add
   subsections but must not remove any.
4. Preserve literal blocks CHARACTER-FOR-CHARACTER: JSON reply schemas, HARD
   CONTRACT blocks, STATUS/VERDICT vocabularies, TRAINING_PID lines. Do NOT
   summarize them. The output should be at least as long as the template.
5. Write the finished prompt to {out_path} with your Write tool. Leave NO
   {{{{PLACEHOLDER}}}} markers behind.
6. After writing, reply with exactly one line: `WROTE {out_path} (<byte-count> bytes)`.

Do not write any file other than {out_path}. Do not touch the codebase.
"""


_AGENT_PROMPT_CONTRACT_PATTERNS = {
    "planner": (
        (r"The harness\s+creates and manages these", "the harness must own version creation"),
        (r"named `V\{N\}_\{short_description\}` \(capital V", "version names must use capital V"),
        (r"(?m)^- New: V\{N\}_\{description\}$", "planner output must preserve the parsed New version field"),
        (r"(?m)^- From: V\{source\}$", "planner output must preserve the parsed source version field"),
        (r"(?m)^- Axis: ", "planner output must preserve the Change Signature axis field"),
        (r"(?m)^- Delta: ", "planner output must preserve the Change Signature delta field"),
        (r"(?m)^- Tier: ", "planner output must preserve the Change Signature tier field"),
        (r"(?m)^- Steps: <int>$", "planner output must preserve the parsed step-budget field"),
    ),
    "executer": (
        (r"The harness creates the Planner-named version", "the harness must create the version"),
        (r"Do not create, copy, rename, or delete version directories", "the Executer must not manage version directories"),
        (r"`V<N>_xxx/`", "version examples must use capital V"),
        (r"(?m)^TRAINING_PID: <integer pid>$", "Executer output must preserve TRAINING_PID"),
        (r"(?m)^TRAINING_LOG: <absolute path to training\.log>$", "Executer output must preserve TRAINING_LOG"),
        (r"(?m)^FIRST_STEP_CONFIRMED: yes \| no — <reason>$", "Executer output must preserve FIRST_STEP_CONFIRMED"),
    ),
    "orchestrator": (
        (r"harness has already created and selected the new version directory", "the harness must create and select the version"),
        (r'"action":\s+"advance" \| "end_cycle" \| "abort"', "orchestrator action vocabulary is missing or changed"),
        (r'"target":\s+"planner" \| "executer" \| "verifier" \| "evaluator" \| "secretary" \| null', "orchestrator target vocabulary is missing or changed"),
        (r'"prompt":', "orchestrator JSON prompt field is missing"),
        (r'"summary":', "orchestrator JSON summary field is missing"),
        (r'"cycle_done":', "orchestrator JSON cycle_done field is missing"),
        (r'"success":', "orchestrator JSON success field is missing"),
        (r'"reason":', "orchestrator JSON reason field is missing"),
    ),
    "evaluator": (
        (r"(?m)^EVAL_VERDICT: GOAL_MET \| GOAL_NOT_MET \| INCONCLUSIVE$", "evaluator verdict vocabulary is missing or changed"),
        (r"(?m)^PRIMARY_METRIC: <float> \| N/A$", "evaluator PRIMARY_METRIC contract is missing or changed"),
        (r"(?m)^EVIDENCE:$", "evaluator EVIDENCE contract is missing"),
        (r"(?m)^JUSTIFICATION:", "evaluator JUSTIFICATION contract is missing"),
    ),
}
_TRAINING_LOG_CONTRACT_ROLES = {"executer", "orchestrator", "verifier"}
_VERIFIER_TERMINAL_STATUSES = (
    "DONE_NORMAL",
    "KILLED_BUDGET",
    "KILLED_EARLY_STOP",
    "CRASHED",
    "ERROR",
)
_VERIFIER_CONTRACT_HEADING = "## Terminal Output Contract"
_VERIFIER_REQUIRED_LITERALS = (
    ("terminal reply is EXACTLY one `STATUS:` line followed by a `PHENOMENA:` … `PHENOMENA_END:` block — nothing else.",
     "verifier must require exactly one terminal STATUS followed by the PHENOMENA block"),
    ("<1-3 paragraphs, OBSERVATIONAL ONLY, concrete numbers when available:",
     "verifier PHENOMENA must require 1-3 observational paragraphs with concrete numbers"),
    ("Omitting it loses that evidence.", "verifier must require PHENOMENA evidence on terminal output"),
    ("never emit `EVAL_VERDICT:` or `FINAL_VERDICT:`", "verifier must not emit evaluator verdict markers"),
)
_LOWERCASE_VERSION_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])v(?:<N>|\{N\}|\d+)(?:_[A-Za-z0-9_-]+|/)"
)
_TRAINING_LOG_RE = re.compile(r"(?<![A-Za-z])training\.log\b")
_STALE_TRAIN_LOG_RE = re.compile(r"(?<![A-Za-z])train\.log\b")


def _template_headings(role: str) -> tuple[list[str], str | None]:
    """Load the source template headings that generated prompts must preserve."""
    module_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(TEMPLATES_DIR, f"{role}.md"),
        os.path.join(module_dir, "templates", f"{role}.md"),
        os.path.join(os.path.dirname(module_dir), "templates", f"{role}.md"),
    )
    for path in dict.fromkeys(candidates):
        try:
            with open(path) as f:
                template = f.read()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return [], f"cannot read role template for contract validation: {exc}"
        headings = re.findall(r"^## .+$", template, re.MULTILINE)
        if not headings:
            return [], "role template has no required headings"
        return headings, None
    return [], "role template is missing; refusing unvalidated prompt"


def _validate_heading_contract(role: str, text: str) -> list[str]:
    """Require every template heading verbatim and in order; extra headings are allowed."""
    required, error = _template_headings(role)
    if error:
        return [error]
    offset = 0
    errors = []
    for heading in required:
        match = re.search(rf"^{re.escape(heading)}$", text[offset:], re.MULTILINE)
        if not match:
            errors.append(f"required heading missing, renamed, or out of order: {heading}")
            continue
        offset += match.end()
    return errors


def _validate_verifier_contract(text: str) -> list[str]:
    """Protect the exact terminal structure consumed by validate_verifier_terminal."""
    errors = []
    headings = list(re.finditer(
        rf"^{re.escape(_VERIFIER_CONTRACT_HEADING)}$", text, re.MULTILINE,
    ))
    if len(headings) != 1:
        errors.append("verifier must contain exactly one ## Terminal Output Contract heading")
        return errors

    section_start = headings[0].end()
    next_heading = re.search(r"^## .+$", text[section_start:], re.MULTILINE)
    section_end = section_start + next_heading.start() if next_heading else len(text)
    section = text[section_start:section_end]

    statuses = tuple(re.findall(r"^STATUS:\s*([A-Z_]+)(?:\s+#.*)?$", section, re.MULTILINE))
    if statuses != _VERIFIER_TERMINAL_STATUSES:
        errors.append(
            "verifier terminal STATUS values must be exactly: "
            + ", ".join(_VERIFIER_TERMINAL_STATUSES)
        )
    declared_statuses = set(re.findall(
        r"^STATUS:\s*([A-Z_]+)(?:\s+#.*)?$", text, re.MULTILINE,
    ))
    unknown_statuses = declared_statuses.difference(_VERIFIER_TERMINAL_STATUSES)
    if unknown_statuses:
        errors.append("verifier declares unsupported STATUS values: " + ", ".join(sorted(unknown_statuses)))
    if len(re.findall(r"^PHENOMENA:$", section, re.MULTILINE)) != 1:
        errors.append("verifier terminal contract must contain one line-anchored PHENOMENA: marker")
    if len(re.findall(r"^PHENOMENA_END:$", section, re.MULTILINE)) != 1:
        errors.append("verifier terminal contract must contain one line-anchored PHENOMENA_END: marker")
    for literal, message in _VERIFIER_REQUIRED_LITERALS:
        if literal not in text:
            errors.append(message)
    return errors


def validate_runtime_agent_prompt(role: str, text: str) -> list[str]:
    """Validate non-negotiable cross-agent runtime prompt contracts."""
    errors = []
    if role not in {name for name, _ in AGENT_ROLES}:
        return [f"unknown runtime role: {role}"]
    if not text.strip():
        return ["prompt is empty"]
    if "{{" in text or "}}" in text:
        errors.append("unresolved template placeholder")
    errors.extend(_validate_heading_contract(role, text))
    lowercase = _LOWERCASE_VERSION_REF_RE.search(text)
    if lowercase:
        errors.append(f"lowercase version reference is forbidden: {lowercase.group(0)}")
    stale_log = _STALE_TRAIN_LOG_RE.search(text)
    if stale_log:
        errors.append("use training.log consistently; train.log is forbidden")
    if role in _TRAINING_LOG_CONTRACT_ROLES and not _TRAINING_LOG_RE.search(text):
        errors.append("required training.log contract is missing")
    for pattern, message in _AGENT_PROMPT_CONTRACT_PATTERNS.get(role, ()):
        if not re.search(pattern, text):
            errors.append(message)
    if role == "verifier":
        errors.extend(_validate_verifier_contract(text))
    return errors


def validate_runtime_agent_prompts(output_dir: str) -> list[str]:
    """Validate every generated agents/<role>.md and return role-qualified errors."""
    errors = []
    for role, _ in AGENT_ROLES:
        path = os.path.join(output_dir, "agents", f"{role}.md")
        try:
            with open(path) as f:
                text = f.read()
        except OSError as exc:
            errors.append(f"{role}: cannot read prompt: {exc}")
            continue
        errors.extend(f"{role}: {error}" for error in validate_runtime_agent_prompt(role, text))
    return errors


def _setup_one_agent(role: str, output_dir: str, timeout: int) -> tuple[str, bool, str]:
    """Run one Claude Code role writer to write agents/<role>.md. Returns (role, ok, detail).

    Uses a fresh (unpinned) session per role via build_claude_cmd, so it inherits
    the selected model. Each call is independent → safe to run in
    parallel threads (subprocess releases the GIL while waiting)."""
    templates_dir = TEMPLATES_DIR
    template_path = os.path.join(templates_dir, f"{role}.md")
    goal_path = os.path.join(output_dir, "goal.md")
    brief_path = os.path.join(output_dir, "PROJECT_BRIEF.md")
    out_path = os.path.join(output_dir, "agents", f"{role}.md")

    if not os.path.isfile(template_path):
        return role, False, f"template missing: {template_path}"

    prompt = _AGENT_WRITER_INSTRUCTIONS.format(
        role=role,
        template_path=template_path,
        goal_path=goal_path,
        brief_path=brief_path,
        out_path=out_path,
    )
    cmd = build_claude_cmd()
    try:
        r = _run_process_group(
            cmd, input_text=prompt, timeout=timeout, cwd=output_dir,
        )
    except subprocess.TimeoutExpired:
        return role, False, f"timed out after {timeout}s"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        return role, False, f"exit {r.returncode}: {(err[0] if err else '')[:200]}"
    # Success is measured by the file landing and preserving the runtime contract,
    # not by trusting the subprocess's stdout.
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        return role, False, "Claude Code worker finished but wrote no output file"
    try:
        with open(out_path) as f:
            generated = f.read()
    except OSError as exc:
        return role, False, f"cannot read generated prompt: {exc}"
    contract_errors = validate_runtime_agent_prompt(role, generated)
    if contract_errors:
        return role, False, "contract validation failed: " + "; ".join(contract_errors)
    return role, True, f"{os.path.getsize(out_path)} bytes"


def setup_agents_parallel(output_dir: str, timeout: int = PER_CALL_TIMEOUT) -> None:
    """Fan out one Claude Code role writer per role to write agents/*.md concurrently.

    Requires goal.md + PROJECT_BRIEF.md to already exist in output_dir. Retries
    a failed role once. Aborts init if a REQUIRED role cannot be written."""
    goal_path = os.path.join(output_dir, "goal.md")
    brief_path = os.path.join(output_dir, "PROJECT_BRIEF.md")
    if not os.path.isfile(goal_path):
        fatal(f"Parallel agent setup needs goal.md, not found at {goal_path}")
    if not os.path.isfile(brief_path):
        warn(f"PROJECT_BRIEF.md not found at {brief_path}; Claude Code role writers will proceed "
             f"with goal.md only.")
    os.makedirs(os.path.join(output_dir, "agents"), exist_ok=True)

    roles = [r for r, _ in AGENT_ROLES]
    required = {r for r, req in AGENT_ROLES if req}
    status(f"Setting up {len(roles)} agents in parallel with Claude Code"
           f"{' / ' + SELECTED_MODEL if SELECTED_MODEL else ' (default model)'}...")

    results: dict[str, tuple[bool, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(roles)) as pool:
        futures = {
            pool.submit(_setup_one_agent, role, output_dir, timeout): role
            for role in roles
        }
        for fut in concurrent.futures.as_completed(futures):
            role, ok, detail = fut.result()
            results[role] = (ok, detail)
            if ok:
                good(f"  ✓ {role}.md — {detail}")
            else:
                warn(f"  ✗ {role}.md — {detail}")

    # Retry failures once (sequentially — failures are rare and often transient).
    failed = [r for r in roles if not results[r][0]]
    if failed:
        status(f"Retrying {len(failed)} failed role(s): {', '.join(failed)}")
        for role in failed:
            _, ok, detail = _setup_one_agent(role, output_dir, timeout)
            results[role] = (ok, detail)
            (good if ok else warn)(f"  {'✓' if ok else '✗'} {role}.md — {detail}")

    missing_required = [r for r in required if not results[r][0]]
    if missing_required:
        fatal(f"Failed to write required agent prompt(s): {', '.join(missing_required)}. "
              f"Cannot proceed. Check Claude Code and the selected model, then retry.")
    ok_count = sum(1 for r in roles if results[r][0])
    good(f"Parallel agent setup complete: {ok_count}/{len(roles)} written.")


# ---------------------------------------------------------------------------
# The main interviewer driver loop
# ---------------------------------------------------------------------------
def run_interviewer(
    output_dir: str,
    spec_path: str | None = None,
    spec_data: dict | None = None,
    resume: bool = False,
) -> None:
    """Walk the interviewer through five labeled states. Agent prompts are
    written by init.py-launched Claude Code role writers; other writes use the interviewer's
    tools. On a normal (non-resume) run the interviewer reads the
    pre-filled research spec at `spec_path` and runs non-interactively."""

    if not os.path.isfile(INTERVIEWER_TEMPLATE):
        fatal(t("templates_missing", path=INTERVIEWER_TEMPLATE))

    session_id = str(uuid.uuid4())
    record_session_uuid("interviewer", session_id, "init_interviewer")
    info(f"Interviewer session UUID: {session_id}")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "agents"), exist_ok=True)

    codebase_path = (spec_data or {}).get("codebase_path", "").strip()

    # Build the initial message: BEGIN_S1 pointing at the pre-filled spec.
    initial_message = (
        f"[BEGIN_S1] INSPECT_CODEBASE\n"
        f"Spec: {spec_path}\n"
        f"Codebase: {codebase_path}\n"
        f"Output dir: {output_dir}\n"
        f"Templates dir: {TEMPLATES_DIR}\n"
    )

    if resume:
        # --resume: skip S1/S2/S3. The init.py controller regenerates agents/*.md in parallel
        # (S5) using existing goal.md + PROJECT_BRIEF.md, then the interviewer
        # starts at S6 (POLISH) to cross-check them.
        goal_path = os.path.join(output_dir, "goal.md")
        brief_path = os.path.join(output_dir, "PROJECT_BRIEF.md")
        if not os.path.isfile(goal_path) or not os.path.isfile(brief_path):
            fatal(f"--resume requires both goal.md and PROJECT_BRIEF.md in {output_dir}")
        with open(goal_path) as f:
            goal_text = f.read()
        with open(brief_path) as f:
            brief_text = f.read()
        brief_len = len(brief_text)
        status(
            f"--resume: skipping S1/S2/S3; regenerating agents/*.md in parallel "
            f"from existing goal.md ({len(goal_text)}B) and "
            f"PROJECT_BRIEF.md ({brief_len}B)"
        )
        # init.py-controlled S5.
        display_state_transition("S5", "WRITE_AGENTS (init.py, parallel Claude Code roles)")
        setup_agents_parallel(output_dir)
        # Interviewer starts at S6 (index 4 in STATES).
        state_idx = 4
        s6_begin = build_state_begin_message("S6", "POLISH", output_dir)
        initial_message = (
            f"{s6_begin}"
            f"\n## EXISTING goal.md (already written; do not modify)\n"
            f"{goal_text}\n"
            f"\n## EXISTING PROJECT_BRIEF.md (already written; do not modify)\n"
            f"{brief_text}\n"
            f"\nNote: This is a --resume run. S1/S2/S3 were skipped (goal.md and "
            f"PROJECT_BRIEF.md already exist) and S5 (WRITE_AGENTS) was just "
            f"performed by the init.py controller via parallel Claude Code role writers. Proceed with S6 "
            f"(POLISH): read every agents/*.md and fix cross-file inconsistencies. "
            f"You must NOT edit goal.md or PROJECT_BRIEF.md.\n"
        )
    else:
        state_idx = 0  # which state we're currently in (0 == S1)

    next_message = initial_message
    first_call = True
    turn = 0

    while turn < MAX_INTERVIEWER_TURNS:
        turn += 1
        info(f"[turn {turn}] sending to interviewer...")
        response = call_interviewer(
            session_id, next_message,
            first_call=first_call,
            system_prompt_file=INTERVIEWER_TEMPLATE,
        )
        first_call = False

        if response is None:
            fatal(t("agent_dead"))

        tag, body = parse_action(response)

        # ─── State transition signals ──────────────────────────────────────
        if tag.startswith("STATE_DONE"):
            # Tag is "STATE_DONE S<n>"; verify it matches our expected state.
            m = re.match(r"STATE_DONE\s+S(\d+)", tag)
            if not m:
                next_message = (
                    f"[ERROR] Malformed STATE_DONE tag: {tag!r}. "
                    f"Expected 'STATE_DONE S<n>'."
                )
                continue
            n = int(m.group(1))
            # `expected` is the LABEL number of the current state (S1/S2/S3/S5/S6),
            # not its list position — labels skip S4, so parse from STATES.
            expected = int(STATES[state_idx][0][1:])
            if n != expected:
                next_message = (
                    f"[ERROR] You emitted STATE_DONE S{n} but I'm tracking S{expected} "
                    f"({STATES[state_idx][1]}). Re-emit the correct STATE_DONE."
                )
                continue

            good(f"[turn {turn}] {tag} — {body[:200]}")
            state_idx += 1
            if state_idx >= len(STATES):
                # S6 may edit generated prompts, so enforce the runtime contracts
                # again before declaring setup complete.
                contract_errors = validate_runtime_agent_prompts(output_dir)
                if contract_errors:
                    fatal("Generated runtime agent prompt contract failed: "
                          + " | ".join(contract_errors))
                good(t("all_done", path=output_dir))
                # Send the terminal acknowledgement.
                call_interviewer(
                    session_id,
                    "[ALL_DONE] Project bootstrap complete. Session ending.",
                    first_call=False,
                    system_prompt_file=INTERVIEWER_TEMPLATE,
                )
                return

            # Begin the next state.
            sid, sname = STATES[state_idx]

            # S5 (WRITE_AGENTS) is performed by the init.py controller via parallel Claude Code role writers
            # rather than by the interviewer, so it runs concurrently and pins
            # every Claude Code role writer to the selected Claude model. After it lands,
            # advance the interviewer straight to S6 (POLISH), which cross-checks
            # the independently-generated files. The interviewer never sees S5.
            if sid == "S5":
                display_state_transition(sid, sname + " (init.py, parallel Claude Code roles)")
                setup_agents_parallel(output_dir)
                state_idx += 1  # skip past the S5 slot
                sid, sname = STATES[state_idx]

            display_state_transition(sid, sname)
            next_message = build_state_begin_message(sid, sname, output_dir)
            continue

        # ─── Action tags ───────────────────────────────────────────────────
        if tag == "ASK_USER":
            display_question(body)
            user_reply = read_user_input()
            next_message = f"[USER_REPLY] {user_reply}"
            continue

        if tag == "CONFIRM_USER":
            key, text = split_first_line(body)
            display_confirm(key, text)
            verdict, edited = confirm_yes_no_edit()
            if verdict == "OK":
                next_message = "[USER_OK]"
            elif verdict == "EDIT":
                next_message = f"[USER_EDIT] {edited}"
            else:
                next_message = "[USER_REJECT]"
            continue

        if tag == "NOTE":
            key, text = split_first_line(body)
            info(f"[NOTE {key}] stored ({len(text)} chars)")
            next_message = "[OK]"
            continue

        if tag == "DELEGATE":
            key, subtask = split_first_line(body)
            info(f"[DELEGATE {key}] → verifier: {subtask[:120]}")
            next_message = "[OK]"
            continue

        if tag == "SHOW":
            display_show(body)
            next_message = "[OK]"
            continue

        if tag == "ABORT":
            fatal(f"Interviewer aborted: {body}")

        # ─── Unknown / malformed ──────────────────────────────────────────
        warn(f"[turn {turn}] unrecognized tag {tag!r}; full response below:")
        print(f"{DIM}{response[:600]}{RESET}")
        next_message = (
            f"[ERROR] Unrecognized first-line tag {tag!r}. Valid tags: "
            f"[ASK_USER], [CONFIRM_USER], [NOTE], [DELEGATE], [SHOW], "
            f"[STATE_DONE S<n>], [ABORT]. The tag must be the first "
            f"non-whitespace token of your response."
        )

    fatal(t("max_turns", n=MAX_INTERVIEWER_TURNS))


def build_state_begin_message(state_id: str, state_name: str, output_dir: str) -> str:
    """Build the [BEGIN_S<n>] message the interviewer receives at state entry."""
    if state_id == "S3":
        return f"[BEGIN_S3] WRITE_GOAL\nOutput dir: {output_dir}\n"
    if state_id == "S5":
        return (
            f"[BEGIN_S5] WRITE_AGENTS\n"
            f"Output dir: {output_dir}\n"
            f"Templates dir: {TEMPLATES_DIR}\n"
            f"Roles to write (in order): planner, executer, verifier, "
            f"evaluator, secretary, orchestrator\n"
        )
    # S2, S6 don't need extra parameters.
    return f"[BEGIN_{state_id}] {state_name}\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize an Easy-Auto-Research for Deep Learning project.")
    parser.add_argument("--output-dir", default=SCRIPT_DIR)
    parser.add_argument("--model")
    parser.add_argument("--spec")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--fresh", action="store_true")
    parser.add_argument("--run-import-preflight", action="store_true",
                        help="Execute the target entry-point import probe (disabled by default).")
    parser.add_argument("--dangerously-skip-agent-permissions", action="store_true",
                        help="Pass Claude Code's permission-bypass flag. Use only in a trusted environment.")
    return parser


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    output_dir = os.path.abspath(args.output_dir)
    if not args.resume and not args.fresh:
        args.resume = (os.path.isfile(os.path.join(output_dir, "goal.md"))
                       and os.path.isfile(os.path.join(output_dir, "PROJECT_BRIEF.md")))

    # Banner.
    header("Easy-Auto-Research for Deep Learning — Project Initializer")
    info(f"Output directory: {output_dir}")

    global SELECTED_MODEL, SKIP_AGENT_PERMISSIONS
    SKIP_AGENT_PERMISSIONS = args.dangerously_skip_agent_permissions

    # ---- Step 1: Claude Code availability ----
    if not check_cli_available(CLAUDE_BIN):
        fatal(t("cli_missing"))
    good("Claude Code is available.")

    # ---- Step 2: model selection (writes <output_dir>/.ar_model) ----
    if args.model:
        ok, err = validate_model_with_cli(args.model)
        if not ok:
            fatal(t("model_validation_failed", model=args.model, err=err))
        SELECTED_MODEL = args.model
        _write_config(output_dir, MODEL_CONFIG_FILENAME, args.model)
        good(t("model_saved", model=args.model, file=MODEL_CONFIG_FILENAME))
    else:
        prompt_model_selection(output_dir)

    # ---- Step 3: research spec (skipped on --resume) ----
    spec_path = None
    spec_data = None
    if not args.resume:
        spec_path, spec_data = prepare_spec(output_dir, args.spec)
        # Pre-flight: probe the codebase's training entry point NOW so an
        # environment-level import blocker surfaces before setup, not after
        # several wasted research cycles. Non-fatal (writes PREFLIGHT.md on fail).
        if args.run_import_preflight:
            preflight_codebase_import(output_dir, spec_data)

    # Drive the interviewer through the five-state sequence.
    if args.resume:
        header("Interviewer driver — RESUME mode (S5 → S6)")
    else:
        header("Interviewer driver — 5 labeled states (S1 → S6)")
    info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    info(f"Per-call timeout: {PER_CALL_TIMEOUT}s, max turns: {MAX_INTERVIEWER_TURNS}")

    run_interviewer(
        output_dir, spec_path=spec_path, spec_data=spec_data, resume=args.resume
    )

    print()


if __name__ == "__main__":
    main()