#!/usr/bin/env python3
"""
Easy-Auto-Research for Deep Learning Harness

Routes messages between 6 agents (orchestrator, planner, executer, verifier, evaluator,
secretary) within each cycle. The orchestrator agent (LLM, fresh session per cycle) owns
every transition decision; the planner/executer/verifier/evaluator/secretary worker agents
keep persistent --resume sessions across cycles. All task-specific intelligence
lives in agent prompts (agents/*.md), generated per-project by init.py.

Usage:
    python harness.py --goal goal.md
    python harness.py --goal goal.md --max-cycles 10 --pause 30
"""

import argparse
import fcntl
import hashlib
import os
import sys
import re
import json
import glob
import math
import time
import uuid
import signal
import shutil
import stat
import subprocess
import threading
from datetime import datetime
from collections import namedtuple
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ORCHESTRATOR_DIR = os.path.dirname(os.path.abspath(__file__))

RESEARCH_LOG = "research_log.md"
AGENT_LOG_FILE = "agent_thoughts.log"

# Cross-cycle KNOWLEDGE artifacts (planner-facing durable memory). Unlike a
# Claude Code session (which can be burned/reset), these are plain files under
# ORCHESTRATOR_DIR that accumulate over the whole run and are re-injected into
# every planner call. They are what stops the loop from re-tuning a knob it
# already exhausted or re-searching a paper it already found.
#   - KNOWLEDGE_DIGEST_FILENAME: human+agent-readable digest (closed axes,
#     tried methods + outcome, cited papers). Appended to by the harness from
#     planner ## Change Signature / ## Analysis and evaluator INSIGHT lines.
#   - PLAN_LEDGER_FILENAME: append-only JSONL, one record per accepted plan
#     (cycle, version, axis, delta, tier). The dedup gate reads this to reject
#     near-duplicate plans.
KNOWLEDGE_DIGEST_FILENAME = ".knowledge_digest.md"
PLAN_LEDGER_FILENAME = ".plan_ledger.jsonl"
# Append-only JSONL, one record per completed-training cycle carrying the
# evaluator-declared PRIMARY_METRIC (a single scalar, "higher is better"). The
# harness never parses the task's own metric files — it only reads the number
# the evaluator chose to declare — so the no-improvement guard stays as
# task-agnostic as the stall-guard. Used to detect "N cycles with no new best".
METRIC_LEDGER_FILENAME = ".metric_ledger.jsonl"

# Worker roles whose sessions persist across cycles (--resume).
# The orchestrator role is special: fresh session per cycle, never persisted
# (see ORCHESTRATOR_ROLE / load_or_init_sessions).
ROLES = ["planner", "executer", "verifier", "evaluator", "secretary"]
ORCHESTRATOR_ROLE = "orchestrator"  # session reset every cycle, never in .sessions.json
DEFAULT_MAX_CYCLE_TURNS = 1000  # effectively no cap
AGENT_HISTORY_DIR = "agent_history"
SESSIONS_FILENAME = ".sessions.json"  # under agents/ dir; persists session IDs across runs

# ---------------------------------------------------------------------------
# Default run configuration. Command-line arguments may override these values.
# Paths are resolved by convention relative to this script's own location
# (ORCHESTRATOR_DIR = the project dir that contains harness.py).
# ---------------------------------------------------------------------------
class RunConfig:
    GOAL_PATH        = None            # resolved at runtime to <ORCHESTRATOR_DIR>/goal.md
    PAUSE            = 30              # seconds between cycles
    MAX_CYCLES       = 0               # 0 = run until GOAL_MET / Ctrl-C
    MAX_STALL_CYCLES = 5               # stop after N consecutive cycles that produce no
                                       # new completed training output (0 = disabled). A
                                       # cycle that actually ran training — even a failed
                                       # or GOAL_NOT_MET one — resets the streak; only pure
                                       # idle/holding cycles (planner checks and ends
                                       # without new results) count toward the limit.
    AGENT_TIMEOUT    = 3600            # seconds per agent call
    MAX_REPLANS      = 3               # consecutive re-plans before a fresh cycle
    MAX_DEDUP_REPROMPTS = 2            # per cycle: times the planner is bounced for
                                       # proposing an exhausted-axis duplicate before
                                       # the harness gives up and lets the plan through
                                       # (0 = disable the plan-dedup gate entirely).
    MAX_NOIMPROVE_CYCLES = 3           # after N consecutive completed-training cycles
                                       # whose declared PRIMARY_METRIC fails to beat the
                                       # best seen so far, hard-inject a mandatory
                                       # arxiv-verified-search directive so the planner
                                       # seeks external help. This NEVER stops the run
                                       # (only the stall-guard can); the streak resets
                                       # the moment a new best metric lands. 0 = disable.
    MAX_CYCLE_TURNS  = DEFAULT_MAX_CYCLE_TURNS
    MODEL            = None            # None → read .ar_model, else Claude Code default
    DANGEROUSLY_SKIP_AGENT_PERMISSIONS = False


SKIP_AGENT_PERMISSIONS = False


# ---------------------------------------------------------------------------
# Per-role helper-skill authorization (hard-coded in the harness — the single source
# of truth for WHICH skills each role may use). The harness injects an explicit
# reminder of exactly these skills into every call to that role (see
# _skill_reminder_block). Skills live in <ORCHESTRATOR_DIR>/Skills/<name>/ and are
# not in Claude Code's auto-discovery path, so an agent only ever uses a skill the
# harness explicitly tells it about here.
#
# To grant/revoke a skill for a role, edit this map — nothing else. A role absent
# from the map (orchestrator, secretary, verifier, evaluator) is reminded that it
# has NO skills and must not invoke any skill script.
# ---------------------------------------------------------------------------
SKILLS_DIRNAME = "Skills"
AGENT_SKILLS = {
    "planner": [
        ("analyze-hpo", "analyze_hpo.py",
         "Correlate prior versions' hyperparameters with the target metric to pick the next value."),
        ("inspect-checkpoint", "inspect_checkpoint.py",
         "Per-tensor weight stats (NaN/Inf, vanishing/exploding) to explain a diverged/plateaued run."),
        ("inspect-batch", "inspect_batch.py",
         "One-batch data inspection (shape, normalization, label balance) to catch data-pipeline issues."),
        ("estimate-vram", "estimate_vram.py",
         "Pre-flight OOM check before proposing a larger batch size."),
        ("arxiv-verified-search", "arxiv_verified_search.py",
         "Find recent, code-backed prior art — only arXiv papers whose repo has >10 stars."),
    ],
    "executer": [
        ("disciplined-edit", "disciplined_edit.py",
         "snapshot (out-of-tree) BEFORE editing, review the minimal diff after, revert a bad edit."),
        ("validate-before-run", "validate_code.py",
         "Static-check edited Python (py_compile + lint, optional bounded import) BEFORE launching."),
    ],
}

# Prompt-only skills (no script) whose SKILL.md body is loaded DIRECTLY into the
# role's context on every call — the agent doesn't invoke them, they are always
# "on". Map: role -> list of skill directory names under Skills/. Used for
# guidance skills like `humanizer` (make the Secretary's human-facing writing
# sound natural). The SKILL.md content is read once at startup and injected.
AGENT_LOADED_SKILLS = {
    "secretary": ["humanizer"],
}
# Cache of loaded SKILL.md bodies, populated lazily. {skill_name: text}
_LOADED_SKILL_CACHE = {}


def _load_skill_body(name):
    """Read and cache a prompt-only skill's SKILL.md body (frontmatter stripped)."""
    if name in _LOADED_SKILL_CACHE:
        return _LOADED_SKILL_CACHE[name]
    path = os.path.join(ORCHESTRATOR_DIR, SKILLS_DIRNAME, name, "SKILL.md")
    text = ""
    try:
        with open(path) as f:
            raw = f.read()
        # strip a leading YAML frontmatter block (--- ... ---)
        if raw.lstrip().startswith("---"):
            after = raw.split("---", 2)
            text = after[2].strip() if len(after) >= 3 else raw.strip()
        else:
            text = raw.strip()
    except OSError as e:
        log(f"Could not load prompt-skill {name}: {e}", "WARN")
    _LOADED_SKILL_CACHE[name] = text
    return text


def _skill_reminder_block(role):
    """Return the hard-coded, per-role skill authorization block injected into every
    call to that role. Covers BOTH kinds of skills:
      - script skills (AGENT_SKILLS): invoked on demand via Bash.
      - loaded skills (AGENT_LOADED_SKILLS): prompt-only, their SKILL.md body is
        embedded directly so it is always active.
    A role with neither is told it has none."""
    skills_root = os.path.join(ORCHESTRATOR_DIR, SKILLS_DIRNAME)
    allowed = AGENT_SKILLS.get(role)
    loaded = AGENT_LOADED_SKILLS.get(role)

    if not allowed and not loaded:
        return (
            "## YOUR SKILLS — NONE\n\n"
            "You are NOT authorized to use any skill in this project. Do not invoke any "
            f"script under `{skills_root}/`. Perform your role using only your normal tools."
        )

    blocks = []
    if allowed:
        lines = [
            "## YOUR SKILLS — the ONLY on-demand skills you may use (authorized by the harness)\n",
            f"These live under `{skills_root}/`. They are yours alone — no other agent may use "
            "them, and you may not use any skill not listed here. Invoke a skill by running its "
            "script with the Bash tool; read its `SKILL.md` for full usage.\n",
        ]
        for name, script, desc in allowed:
            script_path = os.path.join(skills_root, name, "scripts", script)
            lines.append(f"- **{name}** — {desc}\n  `python3 \"{script_path}\"`")
        lines.append(
            "\nOnly these are permitted. Do NOT invoke any other skill directory even if you "
            "discover one on disk."
        )
        blocks.append("\n".join(lines))

    if loaded:
        for name in loaded:
            body = _load_skill_body(name)
            if not body:
                continue
            blocks.append(
                f"## ACTIVE SKILL: {name} (loaded directly — always apply it to your output)\n\n"
                f"This skill is auto-loaded for your role every turn; you do not need to invoke "
                f"anything. Apply its guidance to everything you write.\n\n"
                f"{body}"
            )

    return "\n\n---\n\n".join(blocks)



# Append-only ledger of every session UUID this script ever mints. Lives at
# the orchestrator-dir root so it survives Re-init.md (Re-init.md does NOT
# delete uuid_ledger.jsonl — that's intentional; the ledger is the only way
# to know which Claude session JSONLs under ~/.claude/projects/<...>/ belong
# to this project across reset cycles).
UUID_LEDGER_FILENAME = "uuid_ledger.jsonl"


def _uuid_ledger_path():
    """Return the absolute path to the project's UUID ledger."""
    return os.path.join(ORCHESTRATOR_DIR, UUID_LEDGER_FILENAME)


def _claude_jsonl_path_for(session_id):
    """Return the expected Claude Code session JSONL path for ORCHESTRATOR_DIR."""
    mangled = _mangle_claude(ORCHESTRATOR_DIR)
    return os.path.expanduser(f"~/.claude/projects/{mangled}/{session_id}.jsonl")


def record_session_uuid(role, session_id, source, cycle=None):
    """Append one record to uuid_ledger.jsonl. Best-effort; silently no-ops
    on filesystem errors (the ledger is observability, not correctness-critical).

    Args:
      role:       one of ROLES + ["orchestrator"], or "interviewer" if called from init.py.
      session_id: the UUID string just minted.
      source:    short label naming the call-site that minted it (e.g.
                  "load_or_init_sessions", "cycle_start", "mint_fresh_orchestrator",
                  "start_fresh_session", "bootstrap_AGENTS").
      cycle:     orchestrator cycle number, if applicable (for per-cycle UUIDs).
    """
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "role": role,
            "session_id": session_id,
            "source": source,
            "cycle": cycle,
            "jsonl_path": _claude_jsonl_path_for(session_id),
        }
        with open(_uuid_ledger_path(), "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Never crash the orchestrator on a ledger write failure.
        pass


def _sessions_path():
    """Resolve the path to the persisted sessions file. AGENTS_DIR is set at startup."""
    base = AGENTS_DIR or os.path.join(ORCHESTRATOR_DIR, "agents")
    return os.path.join(base, SESSIONS_FILENAME)


def load_or_init_sessions():
    """Load persisted {role: session_id} from agents/.sessions.json.
    Missing file or missing roles get fresh UUIDs which are then written back."""
    path = _sessions_path()
    sessions = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                sessions = json.load(f)
        except (json.JSONDecodeError, OSError):
            sessions = {}
    changed = False
    for role in ROLES:
        if role not in sessions or not sessions[role]:
            sessions[role] = str(uuid.uuid4())
            record_session_uuid(role, sessions[role], "load_or_init_sessions")
            changed = True
    if changed:
        save_sessions(sessions)
    return sessions


def save_sessions(sessions):
    """Persist {role: session_id} to agents/.sessions.json."""
    path = _sessions_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(sessions, f, indent=2)
    except OSError as e:
        log(f"Failed to persist session IDs: {e}", "WARN")


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
# AGENTS is initialized lazily after AGENTS_DIR is resolved at startup.
# Until then it holds in-memory placeholder UUIDs as a safety net. Only
# persistent worker roles live here; the orchestrator role is allocated
# fresh per cycle inside CycleSession and is never written to .sessions.json.
# Bootstrap in-memory placeholders only. NOT recorded in the ledger — these
# UUIDs are overwritten by load_or_init_sessions() in main() before any
# agent call, so logging them would just produce orphan entries that are
# never used to spawn a worker. Only load_or_init_sessions() and the
# per-cycle / fresh-session paths write to the ledger.
AGENTS = {role: str(uuid.uuid4()) for role in ROLES}
# _initialized is keyed by (role, session_id) so per-cycle orchestrator sessions
# correctly receive --system-prompt-file on their first turn within that cycle.
_initialized = set()  # set of (role, session_id) tuples
_running = True
_current_proc = None  # Tracks the currently running Claude subprocess for SIGINT cleanup
AGENTS_DIR = None  # Set at startup — resolved to project dir or orchestrator dir

# Project-wide Claude model. Resolved at startup (priority: --model > .ar_model > None).
# None = do not pass --model (use the Claude Code default).
SELECTED_MODEL = None
MODEL_CONFIG_FILENAME = ".ar_model"
CLAUDE_BIN = "claude"


def _mangle_claude(cwd):
    """Map an absolute cwd to Claude Code's project-directory name."""
    return "-" + cwd.replace("/", "-").lstrip("-")

# Shared project brief prepended to every agent prompt (see PROJECT_BRIEF.md).
# Loaded once at startup; None means "no brief found, skip injection".
PROJECT_BRIEF_FILENAME = "PROJECT_BRIEF.md"
PROJECT_BRIEF = None

# Team duty description prepended to every agent prompt so each agent
# understands who does what. Lives in templates/ (shared across projects)
# or in the project dir (project-specific override). Loaded once at startup.
GROUP_DUTY_FILENAME = "group_duty.md"
GROUP_DUTY = None


def load_project_brief():
    """Read PROJECT_BRIEF.md from the orchestrator dir. Returns text or None."""
    path = os.path.join(ORCHESTRATOR_DIR, PROJECT_BRIEF_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            text = f.read().strip()
        return text or None
    except OSError:
        return None


def load_group_duty():
    """Read group_duty.md — first from project dir, then from templates/.
    Returns text or None."""
    for base in [ORCHESTRATOR_DIR,
                 os.path.join(ORCHESTRATOR_DIR, "templates")]:
        path = os.path.join(base, GROUP_DUTY_FILENAME)
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    text = f.read().strip()
                if text:
                    return text
            except OSError:
                pass
    return None


# Per-agent constraints extracted from agents/<role>.md at startup.
# Prepended to every agent call via _prepend_persistent_context() so the agent
# sees its Dos/Don'ts constraints right before each task. {role: constraints_text}
AGENT_CONSTRAINTS = {}


def load_agent_constraints():
    """Extract the ## Constraints section from each agent's .md file.
    Returns {role: constraints_text}."""
    constraints = {}
    for role in ROLES:
        path = os.path.join(AGENTS_DIR, f"{role}.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r") as f:
                content = f.read()
        except OSError:
            continue
        m = re.search(r'(## Constraints\n.*?)(?=\n## |\Z)', content, re.DOTALL)
        if m:
            constraints[role] = m.group(1).strip()
    return constraints


# Full goal.md text — loaded once at startup, fed to the orchestrator agent
# every turn so it can judge `success=true` against the actual termination
# clause rather than a paraphrase. None until main() loads it.
GOAL_TEXT = None


def load_goal_text(goal_path):
    """Read goal.md and return its full text. Returns empty string on error."""
    try:
        with open(goal_path, "r") as f:
            return f.read().strip()
    except OSError as e:
        log(f"Could not read goal.md at {goal_path}: {e}", "WARN")
        return ""


# human_comments.txt lives next to goal.md. It is an append-only journal: a
# locked sidecar records the acknowledged cursor and the one active claim.
# Keeping the journal inode untouched is essential because human tools may hold
# an O_APPEND descriptor without participating in our advisory locking.
HUMAN_COMMENTS_FILENAME = "human_comments.txt"
_MESSAGE_STATE_VERSION = 1
_MESSAGE_SIDECAR_SUFFIXES = (".queue.lock", ".queue.state")


def _human_comments_path():
    """Resolve human_comments.txt next to goal.md (project dir)."""
    return os.path.join(ORCHESTRATOR_DIR, HUMAN_COMMENTS_FILENAME)


def _message_channel_paths(path):
    """Return canonical journal/sidecar paths, all inside the project tree."""
    root = os.path.realpath(ORCHESTRATOR_DIR)
    raw = os.path.abspath(os.fspath(path))
    parent = os.path.realpath(os.path.dirname(raw))
    try:
        if os.path.commonpath((root, parent)) != root:
            raise OSError(f"message channel is outside the project directory: {path}")
    except ValueError as e:
        raise OSError(f"invalid message channel path: {path}") from e
    journal = os.path.join(parent, os.path.basename(raw))
    sidecar_base = os.path.join(parent, f".{os.path.basename(raw)}")
    return journal, sidecar_base + _MESSAGE_SIDECAR_SUFFIXES[0], sidecar_base + _MESSAGE_SIDECAR_SUFFIXES[1]


def _open_regular_fd(path, flags, mode=0o600, restrictive=False):
    """Open one regular, single-link file without following a symlink."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe message-channel open requires O_NOFOLLOW")
    fd = os.open(path, flags | os.O_NOFOLLOW, mode)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError(f"unsafe message-channel file: {path}")
        if restrictive:
            os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _message_process_identity(pid=None):
    """Return a PID-reuse-safe Linux process identity for claim ownership."""
    numeric_pid = os.getpid() if pid is None else int(pid)
    try:
        with open(f"/proc/{numeric_pid}/stat") as f:
            raw_stat = f.read()
        # comm is parenthesized and may contain spaces, so fields after it must
        # be indexed separately. starttime is proc(5) field 22.
        fields_after_comm = raw_stat[raw_stat.rfind(")") + 2:].split()
        start_time = fields_after_comm[19]
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
        uid = os.stat(f"/proc/{numeric_pid}").st_uid
        if not start_time or not boot_id:
            return None
        return {
            "pid": numeric_pid,
            "start_time": start_time,
            "boot_id": boot_id,
            "uid": uid,
        }
    except (OSError, TypeError, ValueError, IndexError):
        return None


def _message_owner_is_live(owner):
    """Fail closed unless owner death or PID reuse is positively established."""
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return False
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Permission denial and other inconclusive probes are not proof of death.
        pass
    try:
        with open(f"/proc/{owner['pid']}/stat") as f:
            raw_stat = f.read()
        if raw_stat[raw_stat.rfind(")") + 2:].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    current = _message_process_identity(owner["pid"])
    # An unreadable identity is inconclusive, so preserve the claim. A readable
    # mismatch positively establishes PID reuse and permits stale recovery.
    return True if current is None else current == owner


def _validate_message_state(state, journal_stat):
    """Validate persistent state before trusting offsets or ownership."""
    if not isinstance(state, dict) or state.get("version") != _MESSAGE_STATE_VERSION:
        raise OSError("invalid message-channel state version")
    cursor = state.get("cursor")
    journal = state.get("journal")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise OSError("invalid message-channel cursor")
    if journal != {"dev": journal_stat.st_dev, "ino": journal_stat.st_ino}:
        raise OSError("message journal changed without resetting its sidecar")
    active = state.get("claim")
    if active is None:
        return state
    required = {"token", "owner", "start", "end"}
    if not isinstance(active, dict) or not required <= active.keys():
        raise OSError("invalid message-channel claim")
    owner = active["owner"]
    if (
        not isinstance(active["token"], str) or not active["token"]
        or not isinstance(owner, dict)
        or set(owner) != {"pid", "start_time", "boot_id", "uid"}
        or not isinstance(owner["pid"], int) or isinstance(owner["pid"], bool)
        or not isinstance(owner["uid"], int) or isinstance(owner["uid"], bool)
        or not all(isinstance(owner[key], str) and owner[key] for key in ("start_time", "boot_id"))
        or not isinstance(active["start"], int) or isinstance(active["start"], bool)
        or not isinstance(active["end"], int) or isinstance(active["end"], bool)
        or active["start"] != cursor or active["end"] <= active["start"]
    ):
        raise OSError("invalid message-channel claim fields")
    return state


def _read_message_state(state_path, journal_stat):
    try:
        fd = _open_regular_fd(state_path, os.O_RDONLY, restrictive=True)
    except FileNotFoundError:
        return {
            "version": _MESSAGE_STATE_VERSION,
            "cursor": 0,
            "journal": {"dev": journal_stat.st_dev, "ino": journal_stat.st_ino},
            "claim": None,
        }
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise OSError("message-channel state is too large")
        return _validate_message_state(json.loads(raw.decode("utf-8")), journal_stat)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise OSError(f"invalid message-channel state: {e}") from e


def _write_message_state(state_path, state):
    """Atomically publish state while the stable lock sidecar is held."""
    try:
        existing = _open_regular_fd(state_path, os.O_RDONLY, restrictive=True)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        os.close(existing)
    parent = os.path.dirname(state_path)
    temp_path = f"{state_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = None
    try:
        fd = _open_regular_fd(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, restrictive=True)
        payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write of message-channel state")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp_path, state_path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _cleanup_message_state_temps(state_path):
    """Remove crash leftovers only after the channel lock is held."""
    for candidate in glob.glob(state_path + ".*.tmp"):
        entry = os.lstat(candidate)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise OSError(f"unsafe message-channel temporary sidecar: {candidate}")
        os.unlink(candidate)


def _lock_message_channel(path):
    """Open the journal and acquire its stable cross-process sidecar lock."""
    journal, lock_path, state_path = _message_channel_paths(path)
    journal_fd = _open_regular_fd(journal, os.O_RDONLY | os.O_CREAT)
    lock_fd = None
    try:
        lock_fd = _open_regular_fd(lock_path, os.O_RDWR | os.O_CREAT, restrictive=True)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Cooperative appenders hold an exclusive journal lock across their
        # complete message. Taking the shared lock after the stable sidecar lock
        # prevents claiming an intermediate write without constraining raw
        # O_APPEND writers, whose advisory-lock-free bytes remain append-only.
        fcntl.flock(journal_fd, fcntl.LOCK_SH)
        # Reject replacement (including a symlink swap) of the lock pathname.
        named = os.stat(lock_path, follow_symlinks=False)
        opened = os.fstat(lock_fd)
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"message-channel lock path changed: {lock_path}")
        _cleanup_message_state_temps(state_path)
        return journal, journal_fd, lock_fd, state_path
    except Exception:
        os.close(journal_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        raise


def _unlock_message_channel(journal_fd, lock_fd):
    try:
        fcntl.flock(journal_fd, fcntl.LOCK_UN)
    finally:
        os.close(journal_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


@dataclass
class MessageClaim:
    """Process-bound ownership of one byte range in an append-only channel."""
    token: str
    path: str
    data: bytes
    text: str
    owner_pid: int
    owner_start_time: str
    owner_boot_id: str
    owner_uid: int
    start: int
    end: int
    state: str = "active"

    @property
    def owner(self):
        return {
            "pid": self.owner_pid,
            "start_time": self.owner_start_time,
            "boot_id": self.owner_boot_id,
            "uid": self.owner_uid,
        }


_message_claims_lock = threading.Lock()


def _reset_message_lock_after_fork():
    global _message_claims_lock
    _message_claims_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_message_lock_after_fork)


def _read_journal_range(fd, start, end):
    chunks = []
    offset = start
    while offset < end:
        chunk = os.pread(fd, min(1024 * 1024, end - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    if offset != end:
        raise OSError("message journal shrank while being read")
    return b"".join(chunks)


def _claim_message_file(path, end_marker=None):
    """Claim the next journal range without changing or replacing its inode."""
    with _message_claims_lock:
        journal_fd = lock_fd = None
        try:
            journal, journal_fd, lock_fd, state_path = _lock_message_channel(path)
            journal_stat = os.fstat(journal_fd)
            state = _read_message_state(state_path, journal_stat)
            active = state["claim"]
            if active is not None and _message_owner_is_live(active["owner"]):
                return None
            recovered_stale = active is not None
            state["claim"] = None

            snapshot_end = journal_stat.st_size
            cursor = state["cursor"]
            if cursor > snapshot_end:
                raise OSError("message journal was truncated behind its acknowledged cursor")
            data = _read_journal_range(journal_fd, cursor, snapshot_end)
            if end_marker is not None:
                marker = end_marker.encode("utf-8")
                marker_end = data.find(marker)
                if marker_end < 0:
                    if recovered_stale:
                        _write_message_state(state_path, state)
                    return None
                marker_end += len(marker)
                while marker_end < len(data) and data[marker_end:marker_end + 1] in (b"\r", b"\n"):
                    marker_end += 1
                data = data[:marker_end]
            if not data:
                if recovered_stale:
                    _write_message_state(state_path, state)
                return None
            try:
                text = data.decode("utf-8").strip()
            except UnicodeDecodeError as e:
                if recovered_stale:
                    _write_message_state(state_path, state)
                log(f"Could not claim {journal}: {e}", "WARN")
                return None
            if not text:
                if recovered_stale:
                    _write_message_state(state_path, state)
                return None

            owner = _message_process_identity()
            if owner is None:
                raise OSError("could not establish message-claim process identity")
            token = uuid.uuid4().hex
            end = cursor + len(data)
            state["claim"] = {
                "token": token,
                "owner": owner,
                "start": cursor,
                "end": end,
            }
            _write_message_state(state_path, state)
            return MessageClaim(
                token=token,
                path=journal,
                data=data,
                text=text,
                owner_pid=owner["pid"],
                owner_start_time=owner["start_time"],
                owner_boot_id=owner["boot_id"],
                owner_uid=owner["uid"],
                start=cursor,
                end=end,
            )
        except OSError as e:
            log(f"Could not claim {path}: {e}", "WARN")
            return None
        finally:
            if journal_fd is not None:
                _unlock_message_channel(journal_fd, lock_fd)


def _claim_matches_state(claim, active, owner):
    return (
        claim.state == "active"
        and owner is not None
        and claim.owner == owner
        and isinstance(active, dict)
        and active.get("token") == claim.token
        and active.get("owner") == owner
        and active.get("start") == claim.start
        and active.get("end") == claim.end
    )


def _finish_message_claim(claim, acknowledge):
    if not claim or claim.state != "active":
        return False
    with _message_claims_lock:
        owner = _message_process_identity()
        if claim.owner != owner:
            return False
        journal_fd = lock_fd = None
        try:
            _, journal_fd, lock_fd, state_path = _lock_message_channel(claim.path)
            state = _read_message_state(state_path, os.fstat(journal_fd))
            active = state["claim"]
            if not _claim_matches_state(claim, active, owner):
                return False
            if acknowledge:
                state["cursor"] = active["end"]
            state["claim"] = None
            _write_message_state(state_path, state)
            claim.state = "acknowledged" if acknowledge else "restored"
            return True
        except OSError as e:
            action = "acknowledge" if acknowledge else "restore"
            log(f"Could not {action} {claim.path}: {e}", "WARN")
            return False
        finally:
            if journal_fd is not None:
                _unlock_message_channel(journal_fd, lock_fd)


def _ack_message_claim(claim):
    """Commit one successful delivery by advancing the durable cursor."""
    return _finish_message_claim(claim, acknowledge=True)


def _restore_message_claim(claim):
    """Release a failed claim; its unchanged byte range remains first in FIFO."""
    return _finish_message_claim(claim, acknowledge=False)


def _commit_message_delivery(claim, label):
    """Durably commit delivery, releasing the claim on acknowledgement failure."""
    if claim is None:
        return True
    if _ack_message_claim(claim):
        return True
    restored = _restore_message_claim(claim)
    suffix = "; input restored for retry" if restored else "; claim could not be released"
    log(f"Could not durably acknowledge {label}{suffix}", "ERROR")
    return False


def _append_message_file(path, text):
    """Append one message without ever rewriting the channel journal."""
    payload = text.strip().encode("utf-8")
    if not payload:
        return
    fd = None
    try:
        journal, _, _ = _message_channel_paths(path)
        fd = _open_regular_fd(journal, os.O_RDWR | os.O_CREAT | os.O_APPEND)
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        prefix = b""
        if size and os.pread(fd, 1, size - 1) not in (b"\r", b"\n"):
            prefix = b"\n"
        os.write(fd, prefix + payload + b"\n")
        os.fsync(fd)
    except OSError as e:
        log(f"Could not append message to {path}: {e}", "WARN")
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def consume_human_comments():
    """Exclusively claim current comments for transactional delivery."""
    return _claim_message_file(_human_comments_path())


def _append_human_comment(text):
    """Append a harness-generated directive to the shared journal."""
    _append_message_file(_human_comments_path(), text)


def _read_project_model(project_dir):
    """Return model name from .ar_model in project_dir, or None."""
    path = os.path.join(project_dir, MODEL_CONFIG_FILENAME)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                value = f.read().strip()
            return value or None
        except OSError:
            return None
    return None


def build_claude_cmd(*extra_args, model=None):
    """Build a Claude Code command list, optionally pinning --model.

    Falls back to module-global SELECTED_MODEL if `model` is None. The base
    prefix is preserved exactly so callers concatenating extra args keep working.
    """
    chosen = model if model is not None else SELECTED_MODEL
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if chosen:
        cmd += ["--model", chosen]
    if SKIP_AGENT_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    cmd += list(extra_args)
    return cmd


def append_agent_history(role, prompt, response):
    """Append a prompt/response exchange to the agent's persistent history file."""
    history_dir = os.path.join(ORCHESTRATOR_DIR, AGENT_HISTORY_DIR)
    os.makedirs(history_dir, exist_ok=True)
    path = os.path.join(history_dir, f"{role}.md")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(path, "a") as f:
            f.write(f"\n---\n### [{ts}] User → {role.title()}\n\n")
            f.write(f"{prompt}\n\n")
            f.write(f"### [{ts}] {role.title()} → Response\n\n")
            f.write(f"{response}\n")
    except OSError:
        pass


def load_agent_history(role):
    """Load the agent's full conversation history from disk. Returns string or empty."""
    path = os.path.join(ORCHESTRATOR_DIR, AGENT_HISTORY_DIR, f"{role}.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r") as f:
            content = f.read()
        # Truncate if too long (keep last ~50K chars to fit context)
        if len(content) > 50000:
            content = "...(earlier history truncated)...\n" + content[-50000:]
        return content
    except OSError:
        return ""

# ---------------------------------------------------------------------------
# Named tuples
# ---------------------------------------------------------------------------
GoalConfig = namedtuple("GoalConfig", [
    "monitor_interval_seconds",
    "max_run_time",       # {"type": "steps"|"wall_clock_seconds", "value": int}
    "codebase_path",      # str | None
])

CycleResult = namedtuple("CycleResult", [
    "status",             # "SUCCESS" | "COMPLETED" | "ERROR_REPLAN" | "FULL_TRAIN"
    "error_context",      # str
])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
    """Print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "", "WARN": "\033[33m", "ERROR": "\033[31m",
              "SUCCESS": "\033[32m"}.get(level, "")
    reset = "\033[0m" if prefix else ""
    print(f"[{ts}] {prefix}{level}: {msg}{reset}", flush=True)


_interaction_counter = 0

def log_agent_thought(role, prompt, response, cwd=None):
    """Log agent interaction to both audit log and per-call file in agent_interactions/."""
    global _interaction_counter
    base = cwd or ORCHESTRATOR_DIR
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
    entry = (
        f"\n{'=' * 60}\n"
        f"[{ts}] AGENT: {role.upper()}\n"
        f"{'=' * 60}\n"
        f"--- PROMPT ---\n{prompt}\n"
        f"--- RESPONSE ---\n{response}\n"
    )

    # Append to combined audit log
    log_path = os.path.join(base, AGENT_LOG_FILE)
    try:
        with open(log_path, "a") as f:
            f.write(entry)
    except OSError:
        pass

    # Write individual interaction file to agent_interactions/
    interactions_dir = os.path.join(ORCHESTRATOR_DIR, "agent_interactions")
    try:
        os.makedirs(interactions_dir, exist_ok=True)
        _interaction_counter += 1
        filename = f"{_interaction_counter:04d}_{ts_file}_{role}.md"
        filepath = os.path.join(interactions_dir, filename)
        with open(filepath, "w") as f:
            f.write(f"# {role.upper()} — {ts}\n\n")
            f.write(f"## Prompt\n\n{prompt}\n\n")
            f.write(f"## Response\n\n{response}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Claude CLI wrapper
# ---------------------------------------------------------------------------

def _start_fresh_session(role):
    """Create a fresh session ID and inject conversation history if available.
    Persists the new ID to agents/.sessions.json so it survives across runs."""
    old_id = AGENTS.get(role)
    new_id = str(uuid.uuid4())
    AGENTS[role] = new_id
    if old_id is not None:
        _initialized.discard((role, old_id))
    save_sessions(AGENTS)
    record_session_uuid(role, new_id, "start_fresh_session")
    return new_id


def _build_prompt_with_history(role, prompt):
    """Prepend conversation history to the prompt for context continuity."""
    history = load_agent_history(role)
    if not history:
        return prompt
    return (
        f"## Previous Conversation History\n"
        f"Below is your conversation history from prior cycles. "
        f"Use it to maintain continuity.\n\n"
        f"{history}\n\n"
        f"---\n\n"
        f"## Current Task\n\n"
        f"{prompt}"
    )


HEARTBEAT_INTERVAL_S = 300       # log a heartbeat every 5 min
IDLE_BYTES_TIMEOUT_S = 90        # if stdout silent this long after producing output, assume hung-on-cleanup
EARLY_EXIT_GRACE_S = 2           # SIGTERM, then SIGKILL after this

HUMAN_INTERRUPT_FILENAME = "human_interrupt.txt"
HUMAN_INTERRUPT_MARKER = "**end**"


class HumanInterrupt(Exception):
    """Raised when a human interrupt is detected during an agent call."""
    def __init__(self, message, claim):
        self.message = message
        self.claim = claim
        super().__init__(f"Human interrupt: {message[:100]}")


def _check_human_interrupt():
    """Claim the first non-empty complete interrupt in FIFO order."""
    path = os.path.join(ORCHESTRATOR_DIR, HUMAN_INTERRUPT_FILENAME)
    while True:
        claim = _claim_message_file(path, HUMAN_INTERRUPT_MARKER)
        if not claim:
            return None
        claimed = claim.text
        if not claimed.endswith(HUMAN_INTERRUPT_MARKER):
            _restore_message_claim(claim)
            return None
        message = claimed[:-len(HUMAN_INTERRUPT_MARKER)].strip()
        if message:
            log(f"[HUMAN INTERRUPT] Claimed message ({len(message)} chars) pending delivery")
            return message, claim
        # Empty framing records carry no directive. Commit them rather than
        # restoring the same FIFO prefix forever, then inspect the next record.
        if not _ack_message_claim(claim):
            _restore_message_claim(claim)
            log("Could not acknowledge empty human interrupt", "WARN")
            return None


def _try_extract_orchestrator_json(text):
    """Return parsed dict on a complete valid orchestrator JSON reply, else None.

    Uses the existing _parse_orchestrator_reply() (defined later in this file).
    That function returns {'_error': ..., '_raw': ...} on failure; we treat
    that as 'not yet complete'.
    """
    if not text or "{" not in text:
        return None
    try:
        parsed = _parse_orchestrator_reply(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if "_error" in parsed:
        return None
    return parsed


def _tail_jsonl_assistant_text(jsonl_path, after_offset=0):
    """Read a terminal assistant reply appended to one exact session JSONL.

    Intermediate assistant/tool-use events are deliberately ignored: recovery
    is successful only when the message records an end-of-turn stop reason.
    """
    if not jsonl_path:
        return None
    last_text = None
    try:
        with open(jsonl_path) as f:
            f.seek(after_offset)
            for line in f:
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if ev.get("type") != "assistant":
                    continue
                msg = ev.get("message") or {}
                if msg.get("stop_reason") not in {"end_turn", "stop_sequence"}:
                    continue
                content = msg.get("content", [])
                if isinstance(content, list):
                    parts = [
                        block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    text = "".join(parts).strip()
                    if text:
                        last_text = text
    except (OSError, ValueError):
        return None
    return last_text


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
            proc.poll()  # reap the group leader without treating its exit as group exit
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _clear_current_proc(proc):
    global _current_proc
    if _current_proc is proc:
        _current_proc = None


def _kill_proc(proc):
    """TERM a CLI process group, wait for the group, then KILL survivors."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    if not _wait_process_group_gone(pgid, EARLY_EXIT_GRACE_S, proc):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        _wait_process_group_gone(pgid, EARLY_EXIT_GRACE_S, proc)
    try:
        proc.wait(timeout=0)
    except Exception:
        pass
    _clear_current_proc(proc)


def _run_process_group(cmd, *, input_text, timeout, cwd=None):
    """Run a CLI in a new session and clean its whole group on every exit path."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_proc(proc)
        try:
            proc.communicate(timeout=EARLY_EXIT_GRACE_S)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    if _process_group_exists(proc.pid):
        _kill_proc(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _run_with_heartbeat_impl(cmd, *, input_text, timeout, cwd, role, session_id):
    """Run a Claude CLI subprocess with three robustness layers, returning a
    CompletedProcess-like object. Designed so that hung-but-emitted CLI calls
    (claude -p generates its reply but never closes stdout / never exits)
    cannot stall the harness.

    Layer 1 — Streaming JSON detector (orchestrator role only):
        Read stdout in a background thread. As soon as a complete, valid
        orchestrator JSON object is sitting in stdout, kill the subprocess
        and return immediately. The orchestrator's reply is bounded JSON;
        any bytes after it are noise.

    Layer 2 — Idle-byte timeout (all roles):
        If the subprocess has produced output but stdout has been silent for
        IDLE_BYTES_TIMEOUT_S (default 90 s), assume it is hung in cleanup
        (leaked stdout to a child process, blocked telemetry POST, etc.).
        Kill it and return what we have.

    Layer 3 — JSONL fallback (all roles):
        On hard wall-clock timeout, before raising, try to recover the last
        assistant text directly from the session JSONL at
        ~/.claude/projects/*/<session_id>.jsonl. This is the source of truth;
        claude -p writes it before flushing stdout. If recovery succeeds, we
        return that text instead of raising.

    Heartbeats: emitted every HEARTBEAT_INTERVAL_S so the operator can see
    that long-running calls (e.g. verifier in monitor mode) are still alive.
    """
    global _current_proc
    jsonl_path = _claude_jsonl_path_for(session_id) if session_id else None
    try:
        jsonl_offset = os.path.getsize(jsonl_path) if jsonl_path else 0
    except OSError:
        jsonl_offset = 0
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
        bufsize=1,
    )
    _current_proc = proc
    # Send the prompt and close stdin so Claude can start processing.
    try:
        proc.stdin.write(input_text)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        # Process already died; fall through to the read loop to capture exit.
        pass

    out_buf: List[str] = []
    err_buf: List[str] = []
    last_byte_time = [time.time()]

    def _reader(stream, buf):
        try:
            for chunk in iter(lambda: stream.read(4096), ""):
                if not chunk:
                    break
                buf.append(chunk)
                last_byte_time[0] = time.time()
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, out_buf), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, err_buf), daemon=True)
    t_out.start()
    t_err.start()

    start = time.time()
    next_heartbeat = start + HEARTBEAT_INTERVAL_S
    is_orchestrator = (role == ORCHESTRATOR_ROLE)
    sid_short = session_id[:8] if session_id else "????????"

    while True:
        # Layer 0 — human interrupt: check human_inbox.json every iteration.
        # Highest priority — if the human wants to interrupt, do it immediately.
        # Only interrupt if the process is still running (if it already exited,
        # let the normal clean-exit path return its output).
        if proc.poll() is None:
            interrupt = _check_human_interrupt()
            if interrupt is not None:
                human_msg, claim = interrupt
                log(
                    f"[L0] role={role} session={sid_short} human interrupt detected "
                    f"— killing subprocess and raising HumanInterrupt",
                    "INFO",
                )
                _kill_proc(proc)
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                raise HumanInterrupt(human_msg, claim)

        # Clean exit — process finished on its own, return whatever we have.
        if proc.poll() is not None:
            if _process_group_exists(proc.pid):
                _kill_proc(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            _clear_current_proc(proc)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
                stdout="".join(out_buf),
                stderr="".join(err_buf),
            )

        elapsed = time.time() - start
        idle = time.time() - last_byte_time[0]
        produced = bool(out_buf)

        # Layer 1 — orchestrator only: complete JSON in stdout → kill and return.
        if is_orchestrator and produced:
            parsed = _try_extract_orchestrator_json("".join(out_buf))
            if parsed is not None:
                log(
                    f"[L1] role={role} session={sid_short} complete JSON detected in stdout "
                    f"({len(''.join(out_buf))}B) — killing hung subprocess and returning early",
                    "INFO",
                )
                _kill_proc(proc)
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="".join(out_buf),
                    stderr="".join(err_buf),
                )

        # Layer 2 — partial stdout is not success. Recover only a complete reply
        # written by this invocation after its recorded JSONL offset.
        if produced and idle >= IDLE_BYTES_TIMEOUT_S:
            log(f"[L2] role={role} session={sid_short} stdout idle; validating JSONL reply", "WARN")
            _kill_proc(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            recovered = _tail_jsonl_assistant_text(jsonl_path, jsonl_offset)
            if recovered:
                return subprocess.CompletedProcess(cmd, 0, recovered, "".join(err_buf))
            raise subprocess.TimeoutExpired(cmd, timeout)

        # Heartbeat
        if time.time() >= next_heartbeat:
            log(
                f"[HEARTBEAT] role={role} session={sid_short} "
                f"cwd={os.path.basename(cwd) if cwd else '?'} "
                f"elapsed={int(elapsed)}s idle={int(idle)}s produced={len(''.join(out_buf))}B "
                f"— waiting for reply...",
                "INFO",
            )
            next_heartbeat += HEARTBEAT_INTERVAL_S

        # Layer 3 — hard wall-clock timeout: kill, then try JSONL recovery.
        if elapsed >= timeout:
            log(
                f"[L3] role={role} session={sid_short} hard timeout after {int(elapsed)}s "
                f"— killing and attempting JSONL fallback",
                "WARN",
            )
            _kill_proc(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            recovered = _tail_jsonl_assistant_text(jsonl_path, jsonl_offset)
            if recovered:
                log(
                    f"[L3] recovered {len(recovered)}B from session JSONL — using it instead of raising",
                    "INFO",
                )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=recovered,
                    stderr="".join(err_buf),
                )
            raise subprocess.TimeoutExpired(cmd, timeout)

        time.sleep(0.5)


def _run_with_heartbeat(cmd, *, input_text, timeout, cwd, role, session_id):
    """Run and always clear/terminate the globally tracked subprocess."""
    try:
        return _run_with_heartbeat_impl(
            cmd, input_text=input_text, timeout=timeout, cwd=cwd,
            role=role, session_id=session_id,
        )
    finally:
        proc = _current_proc
        if proc is not None:
            if proc.poll() is None or _process_group_exists(proc.pid):
                _kill_proc(proc)
            else:
                _clear_current_proc(proc)


def _prepend_persistent_context(prompt, role, planner_comments=""):
    """Hard-prepend PROJECT_BRIEF + goal.md to every worker prompt.

    For role == "planner", also append the pending human_comments.txt content
    (and advance its sidecar cursor after success). Other roles do NOT receive human_comments — the
    planner is the single entry point for human course-corrections, and it
    folds them into the next plan that the rest of the pipeline executes.

    Order in the final prompt:
      1. GROUP DUTY          (team roles — who does what)
      2. PROJECT BRIEF       (codebase facts, always re-read)
      3. PROJECT GOAL        (full goal.md — termination criteria, dos/don'ts)
      4. HUMAN COMMENTS      (planner only; one-shot, file is cleared after read)
      5. CONSTRAINTS         (agent's own Dos/Don'ts from agents/<role>.md)
      6. <task body>         (the orchestrator's actual prompt to this worker)
    """
    parts = []
    if GROUP_DUTY:
        parts.append(
            f"## TEAM DUTY (who does what — stay in your lane)\n\n"
            f"{GROUP_DUTY}\n\n"
            f"---"
        )
    if PROJECT_BRIEF:
        parts.append(
            f"## PROJECT BRIEF (always re-read; do not act against this)\n\n"
            f"{PROJECT_BRIEF}\n\n"
            f"---"
        )
    if GOAL_TEXT:
        parts.append(
            f"## PROJECT GOAL (full goal.md — termination criteria, constraints, don'ts)\n\n"
            f"{GOAL_TEXT}\n\n"
            f"---"
        )
    if role == "planner":
        # Carry the prior cycle's verifier PHENOMENA block forward — observational
        # evidence from the most recent training run. Saved by the cycle loop in
        # run_cycle_v2 whenever a verifier reply contains a PHENOMENA block.
        last_phen_path = os.path.join(ORCHESTRATOR_DIR, LAST_PHENOMENA_FILENAME)
        if os.path.isfile(last_phen_path):
            try:
                with open(last_phen_path, "r") as f:
                    phen_text = f.read().strip()
                if phen_text:
                    parts.append(
                        f"## PHENOMENA FROM PRIOR CYCLE (verifier observations from the most recent training run)\n\n"
                        f"{phen_text}\n\n"
                        f"---"
                    )
            except OSError as e:
                log(f"Failed to read {LAST_PHENOMENA_FILENAME}: {e}", "WARN")
        # Durable cross-cycle knowledge: tried methods + outcomes, closed axes,
        # cited prior art. This is the antidote to re-tuning an exhausted knob or
        # re-searching a known paper — it persists in a file, not a Claude Code session.
        digest_text = read_knowledge_digest()
        if digest_text:
            parts.append(
                f"## KNOWLEDGE DIGEST (durable memory across ALL prior cycles — honor it)\n\n"
                f"Methods already tried (with outcome), axes already CLOSED, and prior art\n"
                f"already cited. Do NOT propose a plan that re-tries an exhausted method or\n"
                f"re-opens a closed axis unless you have a NEW mechanism-level reason and say so.\n\n"
                f"{digest_text}\n\n"
                f"---"
            )
        if planner_comments:
            parts.append(
                f"## HUMAN RESEARCHER COMMENTS (one-shot — acknowledged after successful delivery)\n\n"
                f"The human researcher left these directives for THIS planning turn.\n"
                f"Treat them as the highest-priority input — above your own analysis,\n"
                f"above the prior cycle's research_log.md — except where they would\n"
                f"violate goal.md's don'ts (which always win). Quote the relevant\n"
                f"directives back in your `## Analysis` section so the orchestrator\n"
                f"can audit that you saw them.\n\n"
                f"{planner_comments}\n\n"
                f"---"
            )
    # Prepend the agent's own Dos/Don'ts constraints (extracted from its
    # agents/<role>.md at startup) so every call carries a reminder to comply.
    if role in AGENT_CONSTRAINTS:
        parts.append(
            f"## YOUR CONSTRAINTS — Dos and Don'ts (strictly follow these)\n\n"
            f"{AGENT_CONSTRAINTS[role]}\n\n"
            f"---"
        )

    # Hard-coded, per-role skill authorization (AGENT_SKILLS map). Injected on EVERY
    # call so each agent is explicitly reminded of exactly which skills it may use
    # (and non-skill roles are told they have none). This is the harness's
    # authoritative scoping — it does not depend on agents/<role>.md wording and
    # survives template regeneration.
    parts.append(f"{_skill_reminder_block(role)}\n\n---")

    if not parts:
        return prompt
    return "\n\n".join(parts) + "\n\n" + prompt


def call_agent(role, prompt, *, timeout=3600, cwd=None, session_id_override=None,
               session_id_update=None):
    """
    Call a Claude CLI agent and return its text output.

    First call for a (role, session_id) uses --system-prompt-file + --session-id.
    Subsequent calls use --resume. If --resume fails (session expired),
    automatically starts a fresh session with conversation history injected.
    Returns "[AGENT ERROR] ..." on failure (never raises).

    `session_id_override` lets the caller pin a specific session UUID without
    consulting AGENTS[role] or persisting it to .sessions.json. If that UUID is
    burned, `session_id_update` receives its replacement so the owning cycle
    uses the replacement on every later turn.
    """
    if session_id_override is not None:
        session_id = session_id_override
    else:
        session_id = AGENTS[role]
    # Track initialization per (role, session_id) so a fresh per-cycle session
    # for the orchestrator role correctly receives --system-prompt-file on its
    # very first turn within that cycle.
    init_key = (role, session_id)
    first_call = init_key not in _initialized

    system_prompt = os.path.join(AGENTS_DIR, f"{role}.md")

    cmd = build_claude_cmd()

    if first_call:
        if not os.path.exists(system_prompt):
            return f"[AGENT ERROR] System prompt not found: {system_prompt}"
        cmd += ["--system-prompt-file", system_prompt, "--session-id", session_id]
        # Inject history for context continuity on fresh sessions
        effective_prompt = _build_prompt_with_history(role, prompt)
    else:
        cmd += ["--resume", session_id]
        effective_prompt = prompt

    # Claim planner comments atomically. The inflight copy is acknowledged only
    # after a successful agent response and restored on every failure path.
    comments_claim = consume_human_comments() if role == "planner" else None
    planner_comments = comments_claim.text if comments_claim else ""
    effective_prompt = _prepend_persistent_context(effective_prompt, role, planner_comments)

    # Claude session JSONLs are keyed by (session_id, cwd). Pin all agent calls
    # to ORCHESTRATOR_DIR so --resume finds the same session even when the
    # logical working directory (cwd arg) changes between cycles/versions.
    # The target working directory is communicated to the agent via the prompt.
    if cwd and cwd != ORCHESTRATOR_DIR:
        effective_prompt = (
            f"Working directory for this task: {cwd}\n"
            f"All file reads/writes/commands MUST target this directory.\n\n"
            f"{effective_prompt}"
        )

    # Indicators that the session UUID is burned and we must mint a new one.
    # "No conversation found" — JSONL was deleted/never created for --resume.
    # "already in use"        — JSONL exists from a prior failed first-call,
    #                            so --session-id <same uuid> is rejected.
    SESSION_BURNED_MARKERS = ("No conversation found", "already in use")

    def _is_session_burned(stderr_text):
        return any(m in stderr_text for m in SESSION_BURNED_MARKERS)

    def _mint_fresh_session():
        """Mint and publish a replacement UUID before any later turn."""
        nonlocal session_id, init_key
        old_id = session_id
        if session_id_override is not None:
            new_id = str(uuid.uuid4())  # ephemeral; do not persist
            record_session_uuid(role, new_id, "mint_fresh_orchestrator_retry")
            if session_id_update is None:
                raise RuntimeError("orchestrator replacement UUID has no owner update callback")
            session_id_update(new_id)
        else:
            new_id = _start_fresh_session(role)
        _initialized.discard((role, old_id))
        session_id = new_id
        init_key = (role, new_id)
        return new_id

    def _fail(msg):
        """Restore claimed input, log the failure, and return the error string."""
        _restore_message_claim(comments_claim)
        try:
            log_agent_thought(role, effective_prompt, msg, cwd)
        except Exception:
            pass
        return msg

    try:
        result = _run_with_heartbeat(
            cmd,
            input_text=effective_prompt,
            timeout=timeout,
            cwd=ORCHESTRATOR_DIR,
            role=role,
            session_id=session_id,
        )

        # Burn the (role, session_id) slot eagerly on first_call success OR
        # failure: a failed first-call still leaves a JSONL on disk, so the
        # UUID can never be reused with --session-id again.
        if first_call:
            _initialized.add(init_key)

        if result.returncode != 0:
            stderr = result.stderr.strip()[:500] if result.stderr else "no stderr"

            # Recoverable case: session is burned. Mint fresh UUID and retry
            # as a first-call (--system-prompt-file + --session-id) with
            # injected history for context continuity.
            if _is_session_burned(stderr):
                # Secretary: if "already in use", the user is chatting — do NOT
                # mint a fresh session (that would orphan the user's chat).
                # Return the error so run_cycle_v2's "boss goes first" logic
                # can skip the secretary call gracefully.
                if role == "secretary" and "already in use" in stderr:
                    return _fail(f"[AGENT ERROR] {role} session already in use (user is chatting): {stderr}")
                reason = ("expired" if "No conversation found" in stderr
                          else "burned (already in use)")
                log(f"Session for {role} {reason} — starting fresh session with history", "WARN")
                new_id = _mint_fresh_session()
                retry_cmd = build_claude_cmd(
                    "--system-prompt-file", system_prompt,
                    "--session-id", new_id,
                )
                effective_prompt = _build_prompt_with_history(role, prompt)
                effective_prompt = _prepend_persistent_context(effective_prompt, role, planner_comments)
                if cwd and cwd != ORCHESTRATOR_DIR:
                    effective_prompt = (
                        f"Working directory for this task: {cwd}\n"
                        f"All file reads/writes/commands MUST target this directory.\n\n"
                        f"{effective_prompt}"
                    )
                result = _run_with_heartbeat(
                    retry_cmd,
                    input_text=effective_prompt,
                    timeout=timeout,
                    cwd=ORCHESTRATOR_DIR,
                    role=role,
                    session_id=new_id,
                )
                # New UUID is now burned regardless of outcome.
                _initialized.add((role, new_id))
                if result.returncode != 0:
                    stderr = result.stderr.strip()[:500] if result.stderr else "no stderr"
                    # If retry also burned the session, burn again so next call
                    # mints yet another UUID rather than spinning on this one.
                    if _is_session_burned(stderr):
                        _mint_fresh_session()
                    return _fail(f"[AGENT ERROR] {role} exited with code {result.returncode}: {stderr}")
            else:
                return _fail(f"[AGENT ERROR] {role} exited with code {result.returncode}: {stderr}")

        output = result.stdout.strip()

        # Commit queue consumption before publishing output or local delivery
        # side effects. An acknowledgement failure is a failed call, and the
        # released range remains first in FIFO for a later retry.
        if not _commit_message_delivery(comments_claim, "planner comments"):
            return f"[AGENT ERROR] {role} response completed but planner comments were not acknowledged"

        # Save conversation to persistent local history.
        # Use `prompt` (task body only) — not `effective_prompt` which includes
        # PROJECT_BRIEF, GOAL_TEXT, GROUP_DUTY repeated every turn, bloating
        # the history file and causing premature 50K truncation.
        try:
            append_agent_history(role, prompt, output)
        except Exception as e:
            log(f"Could not save {role} conversation history after delivery: {e}", "WARN")
        try:
            log_agent_thought(role, effective_prompt, output, cwd)
        except Exception as e:
            log(f"Could not save {role} thought log after delivery: {e}", "WARN")
        return output

    except HumanInterrupt as hi:
        if first_call:
            _initialized.add(init_key)
        _restore_message_claim(comments_claim)

        log(f"[HUMAN INTERRUPT] Sending human message to {role} via --resume", "INFO")

        human_prompt = (
            f"## HUMAN RESEARCHER INTERRUPT\n\n"
            f"The human researcher has interrupted your current task with this message:\n\n"
            f"> {hi.message}\n\n"
            f"---\n\n"
            f"Incorporate the human's feedback into your current work. "
            f"Then produce your standard output format as normal."
        )

        interrupt_cmd = build_claude_cmd(
            "--resume", session_id,
        )
        try:
            result = _run_with_heartbeat(
                interrupt_cmd,
                input_text=human_prompt,
                timeout=timeout,
                cwd=ORCHESTRATOR_DIR,
                role=role,
                session_id=session_id,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                stderr = result.stderr.strip()[:500] if result.stderr else "no stderr"
                log(f"[HUMAN INTERRUPT] Agent {role} returned non-zero after interrupt: {stderr}", "WARN")
                if any(m in stderr for m in ("No conversation found", "already in use")):
                    _mint_fresh_session()
                _restore_message_claim(hi.claim)
                return f"[AGENT ERROR] {role} failed after human interrupt: {stderr}"
            if not _commit_message_delivery(hi.claim, "human interrupt"):
                return f"[AGENT ERROR] {role} response completed but human interrupt was not acknowledged"
            try:
                append_agent_history(role, human_prompt, output)
            except Exception as e:
                log(f"Could not save {role} interrupt history after delivery: {e}", "WARN")
            try:
                log_agent_thought(role, human_prompt, output, cwd)
            except Exception as e:
                log(f"Could not save {role} interrupt thought log after delivery: {e}", "WARN")
            return output
        except subprocess.TimeoutExpired:
            _restore_message_claim(hi.claim)
            _mint_fresh_session()
            return f"[AGENT ERROR] {role} timed out after human interrupt"
        except Exception as e:
            _restore_message_claim(hi.claim)
            return f"[AGENT ERROR] {role} exception after human interrupt: {e}"

    except subprocess.TimeoutExpired:
        # Timeout leaves a (possibly truncated/corrupted) JSONL on disk under
        # the current session UUID. The next call would either:
        #   (a) try --resume on a corrupt JSONL and fail again, or
        #   (b) try --session-id on a now-existing UUID and get "already in use".
        # Mint a fresh UUID so the next call starts clean. This applies to worker
        # and per-cycle orchestrator sessions; the latter publishes the replacement
        # back into CycleSession before any later turn.
        _mint_fresh_session()
        return _fail(f"[AGENT ERROR] {role} timed out after {timeout}s")
    except Exception as e:
        return _fail(f"[AGENT ERROR] {role} exception: {e}")


def is_agent_error(output):
    """Check if an agent call returned an error."""
    return output.startswith("[AGENT ERROR]")


# ---------------------------------------------------------------------------
# Orchestrator-agent state-machine support
# ---------------------------------------------------------------------------

_ORCH_VALID_ACTIONS = {"advance", "end_cycle", "abort"}
_ORCH_VALID_TARGETS = {"planner", "executer", "verifier", "evaluator", "secretary"}


def _extract_first_json_object(s):
    """
    Return the substring of `s` containing the first complete top-level
    `{...}` JSON object, or None if no balanced object is found.

    Walks the string with a brace-depth counter that respects double-quoted
    string literals and backslash escapes inside them. This is robust against
    `}` characters appearing inside JSON string values (common in this project
    because the orchestrator's `prompt` field often quotes code/config).
    """
    if not s:
        return None
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


# Filename used to carry the most recent verifier PHENOMENA block forward to
# the next cycle's planner. Lives at the project root (ORCHESTRATOR_DIR).
# Overwrite-only — only the most recent cycle's phenomena are retained.
LAST_PHENOMENA_FILENAME = ".last_phenomena.md"


def _extract_phenomena(text):
    """Extract a `PHENOMENA:` ... `PHENOMENA_END:` block from a verifier reply.

    Strategy: find the LAST line-anchored `PHENOMENA:` marker (terminal
    blocks are at the end of replies; verifiers often quote the contract
    earlier in their reply, which we must not pick up). Then find the next
    line-anchored `PHENOMENA_END:` after that marker. Returns the full
    block including the markers, or None if either marker is missing or
    the file appears malformed.
    """
    if not text:
        return None
    # Line-anchored search: marker must start a line (or be at file start).
    # Use rfind so we pick the LAST PHENOMENA: marker — the terminal one.
    needle_start = "\nPHENOMENA:"
    needle_end = "\nPHENOMENA_END:"
    start_idx = text.rfind(needle_start)
    if start_idx == -1:
        # Fall back to file-start case (no leading newline).
        if text.startswith("PHENOMENA:"):
            start_idx = 0
        else:
            return None
    else:
        start_idx += 1  # skip the leading newline so block starts with "PHENOMENA:"
    end_idx = text.find(needle_end, start_idx)
    if end_idx == -1:
        if text.endswith("PHENOMENA_END:"):
            end_idx = len(text) - len("PHENOMENA_END:")
        else:
            return None
    end_idx += 1  # skip the leading newline so block ends with "PHENOMENA_END:"
    # Find end of the PHENOMENA_END: line itself
    line_end = text.find("\n", end_idx + len("PHENOMENA_END:"))
    if line_end == -1:
        line_end = len(text)
    else:
        line_end += 1
    block = text[start_idx:line_end].strip()
    return block or None


def validate_verifier_terminal(text):
    """Return (status, phenomena, error) for the full verifier contract."""
    statuses = re.findall(r"^STATUS:\s*(\S+)\s*$", text or "", re.MULTILINE)
    if len(statuses) != 1:
        return None, None, "missing or ambiguous verifier STATUS"
    status = statuses[0]
    known = {"DONE_NORMAL", "KILLED_BUDGET", "KILLED_EARLY_STOP", "CRASHED", "ERROR"}
    if status not in known:
        return None, None, f"unknown verifier STATUS: {status}"
    phenomena = _extract_phenomena(text)
    if status == "DONE_NORMAL":
        if not phenomena:
            return None, None, "DONE_NORMAL requires a complete PHENOMENA block"
        body = re.sub(r"^PHENOMENA:\s*|\s*PHENOMENA_END:\s*$", "", phenomena,
                      flags=re.DOTALL).strip()
        if not body:
            return None, None, "DONE_NORMAL requires non-empty PHENOMENA observations"
    return status, phenomena, ""


def _verifier_replan_diagnostic(status, output, phenomena=None):
    """Build actionable context that the next Planner receives after verifier failure."""
    if phenomena:
        evidence = phenomena
    else:
        excerpt = (output or "(empty verifier response)").strip()
        evidence = (
            "No complete PHENOMENA block was supplied. Raw verifier output:\n"
            f"{excerpt[:2000]}"
        )
    return (
        f"Verifier reported STATUS: {status}; training did not complete normally and the "
        f"Evaluator was skipped. Diagnose and correct this failure before relaunching.\n"
        f"{evidence}"
    )


def _prepare_replan_attempt(error_context, replan_count, max_replans):
    """Keep a queued diagnostic for its Planner turn, resetting only its counter window."""
    boundary_reached = error_context is not None and replan_count >= max_replans
    if boundary_reached:
        replan_count = 0
    return error_context, replan_count, boundary_reached


def _save_last_phenomena(block):
    """Atomically write the most recent verifier PHENOMENA block to
    `<ORCHESTRATOR_DIR>/.last_phenomena.md`. Overwrite, not append. Best-effort:
    failures are logged but never raise."""
    if not block:
        return
    path = os.path.join(ORCHESTRATOR_DIR, LAST_PHENOMENA_FILENAME)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(block)
            if not block.endswith("\n"):
                f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        log(f"Failed to persist {LAST_PHENOMENA_FILENAME}: {e}", "WARN")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _parse_evaluator_contract(text):
    """Return the one terminal evaluator contract, with no trailing prose."""
    source = text or ""
    starts = list(re.finditer(
        r"^EVAL_VERDICT:[ \t]*(GOAL_MET|GOAL_NOT_MET|INCONCLUSIVE)[ \t]*$",
        source, re.MULTILINE,
    ))
    if len(starts) != 1:
        return None, "evaluator must emit exactly one anchored verdict"
    contract = source[starts[0].start():]
    match = re.fullmatch(
        r"EVAL_VERDICT:[ \t]*(?P<verdict>GOAL_MET|GOAL_NOT_MET|INCONCLUSIVE)[ \t]*\n"
        r"PRIMARY_METRIC:[ \t]*(?P<metric>[^\n]+?)[ \t]*\n"
        r"EVIDENCE:[ \t]*\n"
        r"(?P<evidence>(?:[ \t]+-[ \t]+[^\n]+\n)+)"
        r"JUSTIFICATION:\s*(?P<justification>[^\n]*\S)[ \t]*"
        r"(?:\nINSIGHT:\s*[^\n]*\S[ \t]*)?"
        r"\s*",
        contract,
    )
    if not match:
        return None, "evaluator contract is incomplete or has non-whitespace trailing text"
    metric = match.group("metric").strip()
    if metric != "N/A" and parse_primary_metric(f"PRIMARY_METRIC: {metric}") is None:
        return None, "evaluator contract requires one valid PRIMARY_METRIC"
    return match, ""


def parse_evaluator_verdict(text):
    match, _ = _parse_evaluator_contract(text)
    return match.group("verdict") if match else None


def _validate_eval_verdict_goal_met(text, version_dir=None):
    """Validate the one complete terminal evaluator contract."""
    match, error = _parse_evaluator_contract(text)
    if not match:
        return False, error
    verdict = match.group("verdict")
    if verdict != "GOAL_MET":
        return True, ""
    evidence_lines = re.findall(r"^\s+-\s+(.+?)\s*$", match.group("evidence"), re.MULTILINE)
    if not evidence_lines:
        return False, "GOAL_MET requires at least one evidence file"
    root = os.path.realpath(version_dir) if version_dir else None
    for line in evidence_lines:
        path = re.sub(r"\s+\([^\n]*\)\s*$", "", line).strip()
        if not os.path.isabs(path):
            return False, f"evidence path must be absolute: {path}"
        real = os.path.realpath(path)
        try:
            inside = root is None or os.path.commonpath((root, real)) == root
        except ValueError:
            inside = False
        if (not inside or os.path.islink(path) or not os.path.isfile(path)
                or os.path.getsize(path) <= 0):
            return False, "every GOAL_MET evidence item must be a non-empty regular file inside the current version"
    return True, ""


def _parse_orchestrator_reply(text):
    """
    Parse the orchestrator agent's JSON reply.
    Returns the parsed dict on success, or {"_error": "...", "_raw": text} on failure.
    Accepts either a bare JSON object or one wrapped in a ```json fenced block.
    """
    if not text or not text.strip():
        return {"_error": "empty reply", "_raw": text or ""}

    raw = text.strip()
    # Strip ```json / ``` fence markers textually (don't try to capture the body
    # via regex — the body may contain `}` characters in string values, which
    # would defeat both greedy and non-greedy matchers).
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = raw.strip()

    # Find the first complete top-level JSON object using a brace-depth scanner
    # that respects string literals and escapes. Required because the orchestrator
    # may emit prose around the JSON, and the JSON's `prompt` field commonly
    # contains `}` characters when quoting code/config to workers.
    extracted = _extract_first_json_object(raw)
    if extracted is not None:
        raw = extracted

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"_error": f"json decode failed: {e}", "_raw": text}

    if not isinstance(obj, dict):
        return {"_error": "top-level value is not a JSON object", "_raw": text}

    # Validate required top-level keys.
    for key in ("action", "summary", "reason"):
        if key not in obj:
            return {"_error": f"missing required key '{key}'", "_raw": text}

    action = obj.get("action")
    if action not in _ORCH_VALID_ACTIONS:
        return {"_error": f"invalid action '{action}' (must be one of {sorted(_ORCH_VALID_ACTIONS)})",
                "_raw": text}

    if action == "advance":
        target = obj.get("target")
        if target not in _ORCH_VALID_TARGETS:
            return {"_error": f"action='{action}' requires target in {sorted(_ORCH_VALID_TARGETS)}, got {target!r}",
                    "_raw": text}
        prompt = obj.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"_error": f"action='{action}' requires non-empty 'prompt' string",
                    "_raw": text}

    cycle_done = bool(obj.get("cycle_done", False))
    if (action == "end_cycle") != cycle_done:
        return {"_error": f"cycle_done={cycle_done} inconsistent with action='{action}' "
                          f"(must be true iff action=='end_cycle')",
                "_raw": text}

    # Normalize optional fields with defaults.
    obj.setdefault("target", None)
    obj.setdefault("prompt", None)
    obj.setdefault("success", False)
    obj["cycle_done"] = cycle_done
    obj["success"] = bool(obj.get("success", False))
    return obj


CYCLE_STAGES = ["planner", "executer", "secretary", "verifier", "evaluator"]

@dataclass
class CycleSession:
    """Per-cycle state for the orchestrator agent. Discarded at cycle end."""
    cycle_num: int
    orchestrator_session_id: str
    orchestrator_history: List[Dict[str, Any]] = field(default_factory=list)
    planner_output: Optional[str] = None
    last_worker_role: Optional[str] = None
    last_worker_output: Optional[str] = None
    turns_used: int = 0
    cycle_done: bool = False
    success: bool = False
    stage_index: int = 0
    # The ONLY source of a SUCCESS stop: set True iff the evaluator's own reply
    # literally contained `EVAL_VERDICT: GOAL_MET` AND its cited evidence paths
    # exist on disk. The orchestrator can NEVER set this — it is derived solely
    # from the evaluator worker output (see the evaluator branch in run_cycle_v2).
    eval_goal_met: bool = False
    # Number of times the planner was reprompted this cycle because its plan was
    # a near-duplicate of an already-exhausted search axis (dedup gate). Bounded
    # by MAX_DEDUP_REPROMPTS so a stubborn planner can't loop forever.
    dedup_reprompts: int = 0


def _format_orchestrator_input(session, goal_text, max_turns):
    """Build the prompt body sent to the orchestrator agent for one turn.

    Note: PROJECT_BRIEF and goal.md are intentionally NOT prepended here.
    `call_agent` unconditionally prepends them for every role (orchestrator
    included); adding them again would double them in every turn's prompt.
    The `goal_text` parameter is therefore unused in the body — kept for
    backwards compatibility with callers.
    """
    del goal_text  # see docstring
    parts = []
    parts.append("## Fixed Worker Order (harness-controlled)\n")
    parts.append("planner -> executer -> secretary -> verifier -> evaluator (when STATUS: DONE_NORMAL)\n")
    parts.append("Routing is deterministic. You MUST set target to the expected next stage below.\n\n")
    expected = CYCLE_STAGES[session.stage_index] if session.stage_index < len(CYCLE_STAGES) else "(end_cycle expected)"
    parts.append(f"## Expected Next Target\n`{expected}`\n\n")
    parts.append(f"## Cycle Number\nCycle {session.cycle_num}\n\n")
    parts.append(f"## Orchestrator Turn\nTurn {session.turns_used + 1} of max {max_turns}\n\n")

    if session.planner_output:
        parts.append("## This Cycle's Planner Output (cached)\n")
        parts.append(session.planner_output.strip())
        parts.append("\n\n---\n\n")

    if session.orchestrator_history:
        HISTORY_CAP = 50
        recent_history = session.orchestrator_history[-HISTORY_CAP:]
        if len(session.orchestrator_history) > HISTORY_CAP:
            parts.append(f"## Your Prior JSON Replies This Cycle (showing last {HISTORY_CAP} of {len(session.orchestrator_history)})\n")
        else:
            parts.append("## Your Prior JSON Replies This Cycle\n")
        for entry in recent_history:
            parts.append(f"- Turn {entry['turn']}: {json.dumps(entry['reply'], ensure_ascii=False)}\n")
        parts.append("\n")

    parts.append("## Previous Worker Output (your input to react to)\n")
    if session.last_worker_role is None:
        parts.append("(none — start of cycle; you must call planner first)\n")
    else:
        parts.append(f"Role: {session.last_worker_role}\n\n")
        parts.append("Output:\n")
        parts.append(session.last_worker_output or "(empty)")
        parts.append("\n")

    parts.append("\n---\n\n")
    parts.append("## Your Task\n")
    parts.append("Reply with a single JSON object per the schema in your system prompt. "
                 "No surrounding prose. No markdown unless using a ```json fence.\n")
    return "".join(parts)


def run_orchestrator_agent_turn(session, goal_text, agent_timeout, max_turns):
    """
    Call the orchestrator agent once and return the parsed reply dict.
    On parse failure, returns the error dict (caller handles re-ask).
    """
    prompt = _format_orchestrator_input(session, goal_text, max_turns)
    raw = call_agent(
        ORCHESTRATOR_ROLE,
        prompt,
        timeout=agent_timeout,
        cwd=None,
        session_id_override=session.orchestrator_session_id,
        session_id_update=lambda new_id: setattr(session, "orchestrator_session_id", new_id),
    )
    if is_agent_error(raw):
        return {"_error": f"agent call failed: {raw}", "_raw": raw}
    return _parse_orchestrator_reply(raw)


# ---------------------------------------------------------------------------
# Goal config parsing
# ---------------------------------------------------------------------------

def _fallback_goal_config(goal_path, defaults):
    """Deterministic fallback when the LLM parse of goal.md fails or returns empty.

    The authoritative codebase_path lives in research_spec.json (written at setup);
    read it directly rather than trusting a flaky one-shot Claude Code parse. Numeric
    fields are best-effort scraped from goal.md, else left at defaults. This keeps the
    version-tree mechanism working even when Claude Code returns nothing on stdout.
    """
    project_dir = os.path.dirname(os.path.abspath(goal_path))
    codebase = None
    spec_path = os.path.join(project_dir, "research_spec.json")
    if os.path.isfile(spec_path):
        try:
            with open(spec_path) as f:
                spec = json.load(f)
            cb = (spec.get("codebase_path") or "").strip()
            if cb and os.path.isdir(cb):
                codebase = cb
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    # Last resort: scrape an absolute path from goal.md's Codebase section.
    if codebase is None and os.path.isfile(goal_path):
        try:
            with open(goal_path) as f:
                gc = f.read()
            m = re.search(r"[Cc]odebase[^\n]*\n[^\n]*?(`?)(/[^\s`]+)\1", gc)
            if m and os.path.isdir(m.group(2)):
                codebase = m.group(2)
        except OSError:
            pass
    if codebase:
        log(f"Fallback goal config: codebase_path resolved to {codebase} "
            f"(from research_spec.json / goal.md)", "INFO")
    return GoalConfig(
        monitor_interval_seconds=defaults.monitor_interval_seconds,
        max_run_time=defaults.max_run_time,
        codebase_path=codebase,
    )


def parse_goal_config(goal_path):
    """
    Parse goal.md into structured config via a single Claude call.
    Returns GoalConfig with safe defaults on failure.
    """
    defaults = GoalConfig(
        monitor_interval_seconds=30,
        max_run_time={"type": "steps", "value": 100},
        codebase_path=None,
    )

    if not os.path.exists(goal_path):
        log(f"Goal file not found: {goal_path}", "ERROR")
        return defaults

    with open(goal_path, "r") as f:
        goal_content = f.read()

    prompt = f"""Parse the following goal.md and extract exactly these fields as JSON:

{{
  "monitor_interval_seconds": <integer seconds between monitor checks>,
  "max_run_time": {{
    "type": "steps or wall_clock_seconds",
    "value": <integer>
  }},
  "codebase_path": "<absolute path string, or null if codebase is not local>"
}}

Rules:
- monitor_interval_seconds: parse "every 30 seconds" → 30, "every 2 minutes" → 120. Default 30 if unclear.
- max_run_time: parse "100 gradient descent steps" → {{"type":"steps","value":100}}.
  parse "max 30 minutes" → {{"type":"wall_clock_seconds","value":1800}}.
  If both step-based and time-based are given, prefer step-based.
- codebase_path: if the codebase section says "search on internet", "github", or similar non-path text, return null.
  If it is an absolute path, return it exactly.
- Return ONLY the JSON object, no explanation, no markdown fences.

--- GOAL.MD CONTENT ---
{goal_content}"""

    try:
        result = _run_process_group(
            build_claude_cmd(), input_text=prompt, timeout=60,
            cwd=ORCHESTRATOR_DIR,
        )
        if result.returncode != 0:
            log("Failed to parse goal config via Claude, using research_spec.json fallback", "WARN")
            return _fallback_goal_config(goal_path, defaults)

        raw = result.stdout.strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```\w*\n?', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\n?```$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()

        if not raw:
            log("Goal config parse returned empty output, using research_spec.json fallback", "WARN")
            return _fallback_goal_config(goal_path, defaults)

        data = json.loads(raw)

        monitor = int(data.get("monitor_interval_seconds", 30))
        max_run = data.get("max_run_time", {"type": "steps", "value": 100})
        codebase = data.get("codebase_path")

        # Validate
        if monitor < 5:
            monitor = 5
        if monitor > 600:
            monitor = 600
        if not isinstance(max_run, dict) or "type" not in max_run or "value" not in max_run:
            max_run = {"type": "steps", "value": 100}
        # value may be None when the user delegated time_budget — fall back to default 100
        try:
            max_run["value"] = int(max_run["value"])
        except (TypeError, ValueError):
            log(f"max_run.value is not a number ({max_run.get('value')!r}) — using default 100", "WARN")
            max_run["value"] = 100
        if codebase and not os.path.isdir(codebase):
            log(f"Codebase path does not exist: {codebase}", "WARN")
            codebase = None
        # If the LLM parse dropped codebase_path but research_spec.json has a valid
        # one, recover it — versioning must not silently disable on a parse miss.
        if not codebase:
            recovered = _fallback_goal_config(goal_path, defaults).codebase_path
            if recovered:
                log("Recovered codebase_path from research_spec.json "
                    "(LLM parse omitted it)", "INFO")
                codebase = recovered

        return GoalConfig(
            monitor_interval_seconds=monitor,
            max_run_time=max_run,
            codebase_path=codebase,
        )

    except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
        log(f"Goal config parse error: {e}, using research_spec.json fallback", "WARN")
        return _fallback_goal_config(goal_path, defaults)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def get_research_log(cwd=None):
    """Read the full research_log.md content."""
    path = os.path.join(cwd or ORCHESTRATOR_DIR, RESEARCH_LOG)
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read()
    return "Research log is empty (first cycle)."


def get_last_cycle_number(cwd=None):
    """Parse research_log.md for the highest cycle number to support resume."""
    path = os.path.join(cwd or ORCHESTRATOR_DIR, RESEARCH_LOG)
    if not os.path.exists(path):
        return 0
    with open(path, "r") as f:
        content = f.read()
    matches = re.findall(r"## Cycle (\d+)", content)
    if matches:
        return max(int(m) for m in matches)
    return 0


def _pidfd_available():
    return callable(getattr(os, "pidfd_open", None)) and callable(
        getattr(signal, "pidfd_send_signal", None)
    )


def _pidfd_points_to(pidfd, pid):
    """Confirm that a pidfd still names the expected live numeric PID."""
    try:
        with open(f"/proc/self/fdinfo/{pidfd}") as f:
            fields = dict(
                line.rstrip("\n").split(":", 1) for line in f if ":" in line
            )
        return int(fields.get("Pid", "-1").strip()) == int(pid)
    except (OSError, TypeError, ValueError):
        return False


def process_identity(pid, pidfd=None):
    """Read identity from /proc while a pidfd pins the process instance."""
    try:
        numeric_pid = int(pid)
        if pidfd is not None and not _pidfd_points_to(pidfd, numeric_pid):
            return None
        proc = f"/proc/{numeric_pid}"
        with open(os.path.join(proc, "stat")) as f:
            start_time = f.read().split()[21]
        cwd = os.path.realpath(os.readlink(os.path.join(proc, "cwd")))
        uid = os.stat(proc).st_uid
        with open(os.path.join(proc, "cmdline"), "rb") as f:
            command = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        if pidfd is not None and not _pidfd_points_to(pidfd, numeric_pid):
            return None
        return {"pid": numeric_pid, "start_time": start_time, "uid": uid,
                "cwd": cwd, "command": command}
    except (OSError, TypeError, ValueError, IndexError):
        return None


def _receipt_shape_valid(receipt):
    required = {"pid", "start_time", "uid", "cwd", "command"}
    return (
        isinstance(receipt, dict)
        and required <= receipt.keys()
        and isinstance(receipt["pid"], int)
        and not isinstance(receipt["pid"], bool)
        and isinstance(receipt["uid"], int)
        and not isinstance(receipt["uid"], bool)
        and all(isinstance(receipt[key], str) and receipt[key]
                for key in ("start_time", "cwd", "command"))
    )


def _open_verified_pidfd(receipt, workspace):
    """Open and validate one process identity; return its pinned pidfd or None."""
    if not _receipt_shape_valid(receipt) or not _pidfd_available():
        return None
    try:
        pidfd = os.pidfd_open(receipt["pid"], 0)
    except (OSError, TypeError, ValueError):
        return None
    try:
        current = process_identity(receipt["pid"], pidfd=pidfd)
        if not current or any(
            current[key] != receipt[key]
            for key in ("start_time", "uid", "cwd", "command")
        ):
            raise ValueError("process identity changed")
        root = os.path.realpath(os.fspath(workspace))
        if os.path.commonpath((root, current["cwd"])) != root:
            raise ValueError("process cwd is outside workspace")
        return pidfd
    except (OSError, TypeError, ValueError):
        os.close(pidfd)
        return None


def pid_receipt_matches(receipt, workspace):
    pidfd = _open_verified_pidfd(receipt, workspace)
    if pidfd is None:
        return False
    os.close(pidfd)
    return True


def signal_pid_receipt(receipt, workspace, sig=signal.SIGINT):
    """Validate and signal through one pidfd; never fall back to os.kill(pid)."""
    pidfd = _open_verified_pidfd(receipt, workspace)
    if pidfd is None:
        return False
    try:
        signal.pidfd_send_signal(pidfd, sig, None, 0)
        return True
    except OSError:
        return False
    finally:
        os.close(pidfd)


def interruptible_sleep(seconds):
    """Sleep in 1-second intervals so SIGINT can break it."""
    for _ in range(int(seconds)):
        if not _running:
            break
        time.sleep(1)


# ---------------------------------------------------------------------------
# Version directory management
# ---------------------------------------------------------------------------

# Output-structure convention (hard-coded target layout):
#   <parent>/<project-dir>      — Easy-Auto-Research for Deep Learning runtime (harness, agents, logs, sessions)
#   <parent>/codebase      — the CodeBase (left in place; config.codebase_path)
#   <parent>/WorkSpace/    — ALL versioned experiment dirs live here:
#                            WorkSpace/V1_baseline, WorkSpace/V2_xxx, ...
# Version dirs are named V<N>_<description> (capital V). The first one is V1_baseline.
WORKSPACE_DIRNAME = "WorkSpace"
VERSION_GLOB = "V*"
VERSION_RE = r"V(\d+)"
VERSION_NAME_RE = re.compile(r"^V[1-9][0-9]*_[A-Za-z0-9][A-Za-z0-9_-]*$")
FIRST_VERSION_NAME = "V1_baseline"


def workspace_dir_for(goal_path):
    """Resolve the WorkSpace/ directory that holds all version dirs.

    Layout: WorkSpace/ is a SIBLING of the project dir (the dir containing
    goal.md), i.e. <parent-of-project>/WorkSpace. Created if absent.
    """
    project_dir = os.path.dirname(os.path.abspath(goal_path))
    parent = os.path.dirname(project_dir)
    ws = os.path.join(parent, WORKSPACE_DIRNAME)
    return ws


def scan_versions(base_dir):
    """
    Scan for V*/ version directories under base_dir (the WorkSpace/ dir).
    Returns sorted list of dicts: [{"name": "V1_baseline", "path": "...", ...}, ...]
    """
    if not base_dir or not os.path.isdir(base_dir):
        return []
    root = os.path.realpath(base_dir)
    pattern = os.path.join(root, VERSION_GLOB)
    versions = []
    for d in glob.glob(pattern):
        name = os.path.basename(d)
        if (not VERSION_NAME_RE.fullmatch(name) or os.path.islink(d)
                or not os.path.isdir(d) or _find_nested_symlink(d)):
            continue
        real = os.path.realpath(d)
        try:
            if os.path.commonpath((root, real)) != root:
                continue
        except ValueError:
            continue
        match = re.fullmatch(r"V([1-9][0-9]*)_[A-Za-z0-9][A-Za-z0-9_-]*", name)
        versions.append({
            "name": name,
            "path": real,
            "num": int(match.group(1)),
            "mtime": datetime.fromtimestamp(os.path.getmtime(d)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        })
    versions.sort(key=lambda v: v["num"])
    return versions


def result_signatures(base_dir):
    """Return content hashes for non-empty result files under version dirs."""
    signatures = {}
    if not base_dir or not os.path.isdir(base_dir):
        return signatures
    for version in scan_versions(base_dir):
        for path in glob.glob(os.path.join(version["path"], "**", "results.jsonl"), recursive=True):
            try:
                digest = hashlib.sha256()
                size = 0
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
            except OSError:
                continue
            if size:
                relpath = os.path.relpath(path, base_dir)
                signatures[relpath] = digest.hexdigest()
    return signatures


def versions_with_training_output(base_dir):
    """Return the set of version-dir names under base_dir that contain at least one
    non-empty results.jsonl (i.e. a run that actually produced training metrics).

    This is a deliberately task-agnostic signal for the stall-guard: it does NOT
    parse or judge the metrics (that would couple the harness to a specific task's
    metric format). It only answers "did a real training run land any results here?"
    A cycle that creates a new such version — even one whose run failed, crashed, or
    was judged GOAL_NOT_MET — counts as genuine progress and resets the stall streak.
    Pure idle/holding cycles (planner checks human_comments.txt and ends the cycle
    without launching training) add no new results-bearing version and thus count as
    a stall. Best-effort; never raises.
    """
    names = set()
    if not base_dir or not os.path.isdir(base_dir):
        return names
    for d in glob.glob(os.path.join(base_dir, VERSION_GLOB)):
        if not os.path.isdir(d):
            continue
        # any train_output_*/results.jsonl (or a top-level results.jsonl) that is non-empty
        hit = False
        for rj in glob.glob(os.path.join(d, "**", "results.jsonl"), recursive=True):
            try:
                if os.path.getsize(rj) > 0:
                    hit = True
                    break
            except OSError:
                continue
        if hit:
            names.add(os.path.basename(d))
    return names


def parse_version_directive(planner_output):
    """
    Parse the VERSION directive from planner output.
    Looks for:
        ## Version
        - New: V2_with_label_smoothing
        - From: V1_baseline

    Returns (new_name, from_name) or None if not found.
    """
    match = re.search(
        r"##\s*Version\s*\n"
        r"\s*-\s*New:\s*(\S+)\s*\n"
        r"\s*-\s*From:\s*(\S+)",
        planner_output,
    )
    if not match:
        return None
    names = (match.group(1).strip(), match.group(2).strip())
    if not all(VERSION_NAME_RE.fullmatch(name) for name in names):
        return None
    return names


def parse_change_signature(planner_output):
    """Parse the planner's `## Change Signature` block.

    Expected shape (planner-declared, so it is project-agnostic — the harness
    never has to understand the domain's hyperparameters):

        ## Change Signature
        - Axis: irm_lambda
        - Delta: 100 -> 1000
        - Tier: refine

    Returns a dict {axis, delta, tier} (values lowercased/stripped; missing
    fields become ""). Returns None if no Change Signature block is present.
    """
    block = re.search(
        r"##\s*Change\s+Signature\s*\n(.*?)(?:\n##\s|\Z)",
        planner_output,
        re.DOTALL | re.IGNORECASE,
    )
    if not block:
        return None
    body = block.group(1)

    def _field(name):
        m = re.search(rf"-\s*{name}\s*:\s*(.+)", body, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    axis = _field("Axis")
    delta = _field("Delta")
    tier_raw = _field("Tier").lower().strip()
    # Normalize tier to the known vocabulary. Planner prompts often show tiers
    # in backticks or with explanatory text (`refine` (...)); accept that.
    m = re.search(r"\b(refine|transplant|novel)\b", tier_raw)
    tier = m.group(1) if m else ""
    return {"axis": axis, "delta": delta, "tier": tier}


def _normalize_axis(axis):
    """Collapse an axis label to a stable key for duplicate detection.
    Lowercase, strip, drop surrounding backticks/quotes. Domain-agnostic:
    two plans touching the same declared axis name collide regardless of what
    the axis means."""
    return axis.strip().strip("`'\"").lower()


def load_plan_ledger():
    """Return the list of prior accepted-plan records (JSONL), oldest first.
    Each record: {cycle, version, axis, delta, tier}. Empty list if absent."""
    path = os.path.join(ORCHESTRATOR_DIR, PLAN_LEDGER_FILENAME)
    records = []
    if not os.path.isfile(path):
        return records
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log(f"Could not read {PLAN_LEDGER_FILENAME}: {e}", "WARN")
    return records


def append_plan_ledger(cycle_num, version_name, sig):
    """Append one accepted plan's Change Signature to the ledger."""
    path = os.path.join(ORCHESTRATOR_DIR, PLAN_LEDGER_FILENAME)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cycle": cycle_num,
        "version": version_name,
        "axis": sig.get("axis", ""),
        "delta": sig.get("delta", ""),
        "tier": sig.get("tier", ""),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"Could not append to {PLAN_LEDGER_FILENAME}: {e}", "WARN")


def check_plan_duplicate(sig, ledger):
    """Decide whether a proposed plan is a low-value near-duplicate.

    Project-agnostic policy (keyed only on planner-declared fields):
      - Only `refine`-tier plans can be duplicates. `transplant`/`novel` tiers
        (applying a method prior or a literature idea) are never blocked — those
        are exactly the exploration we want, even on a familiar axis.
      - A `refine` plan is a duplicate if the SAME axis already has >= 3 prior
        `refine` records with a DIFFERENT delta (i.e. the axis has been sampled
        repeatedly). This is the harness-level backstop for the planner's own
        axis-closure rule: prompts drift over long runs, this check does not.

    Returns (is_dup, reason). reason is a short human string for the reprompt.
    """
    tier = sig.get("tier", "")
    if tier and tier != "refine":
        return False, ""
    axis = _normalize_axis(sig.get("axis", ""))
    if not axis:
        return False, ""  # can't judge without an axis label; let it through
    delta = sig.get("delta", "").strip().lower()
    prior_same_axis = [
        r for r in ledger
        if _normalize_axis(r.get("axis", "")) == axis
        and (r.get("tier", "") == "refine" or not r.get("tier", ""))
    ]
    # Exact repeat of the same axis+delta is always a duplicate.
    for r in prior_same_axis:
        if r.get("delta", "").strip().lower() == delta and delta:
            return True, (
                f"axis '{sig.get('axis','')}' with delta '{sig.get('delta','')}' "
                f"was already tried in cycle {r.get('cycle')}"
            )
    if len(prior_same_axis) >= 3:
        sampled = ", ".join(
            f"{r.get('delta','?')}(c{r.get('cycle','?')})" for r in prior_same_axis[-4:]
        )
        return True, (
            f"axis '{sig.get('axis','')}' has already been refined {len(prior_same_axis)}x "
            f"[{sampled}] with no breakthrough — treat it as CLOSED and switch to a "
            f"structural change / method prior / literature, or raise the tier to "
            f"'transplant'/'novel' if this genuinely applies a new method"
        )
    return False, ""


def read_knowledge_digest():
    """Return the current knowledge-digest text (or "" if none yet)."""
    path = os.path.join(ORCHESTRATOR_DIR, KNOWLEDGE_DIGEST_FILENAME)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError as e:
        log(f"Could not read {KNOWLEDGE_DIGEST_FILENAME}: {e}", "WARN")
        return ""


def append_knowledge_digest(header, body):
    """Append a timestamped entry to the knowledge digest. `header` is a short
    tag (e.g. 'TRIED', 'AXIS_CLOSED', 'INSIGHT'); `body` is the detail line."""
    if not body or not body.strip():
        return
    path = os.path.join(ORCHESTRATOR_DIR, KNOWLEDGE_DIGEST_FILENAME)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Initialize with a heading the first time.
    init = not os.path.isfile(path)
    try:
        with open(path, "a") as f:
            if init:
                f.write(
                    "# Knowledge Digest (durable cross-cycle memory)\n\n"
                    "Accumulated by the harness across cycles: methods tried and their\n"
                    "outcome, axes declared closed, and cited prior art. Injected into\n"
                    "every planner turn. Do not re-try an exhausted method or re-open a\n"
                    "closed axis without a new mechanism-level reason.\n\n"
                )
            f.write(f"- [{ts}] **{header}**: {body.strip()}\n")
    except OSError as e:
        log(f"Could not append to {KNOWLEDGE_DIGEST_FILENAME}: {e}", "WARN")


def _extract_axis_closures(planner_output):
    """Pull any `AXIS_CLOSED: ...` lines the planner wrote in ## Analysis so the
    harness can persist them to the digest. Returns a list of strings."""
    return [
        m.strip()
        for m in re.findall(r"AXIS_CLOSED:\s*(.+)", planner_output, re.IGNORECASE)
    ]


def parse_primary_metric(evaluator_output):
    """Parse the evaluator's optional `PRIMARY_METRIC:` contract line.

    Shape (evaluator-declared, so the harness never has to understand the task's
    metric files — mirrors how EVAL_VERDICT/INSIGHT are declared, not parsed):

        PRIMARY_METRIC: 0.6434

    Convention: a single float where HIGHER IS BETTER (the evaluator normalizes
    to that if the native metric is a loss). Returns the float, or None if the
    line is absent/unparseable (in which case the no-improve guard skips this
    cycle rather than guessing)."""
    if not evaluator_output:
        return None
    matches = re.findall(
        r"^PRIMARY_METRIC:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$",
        evaluator_output, re.MULTILINE,
    )
    if len(matches) != 1:
        return None
    try:
        value = float(matches[0])
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def load_metric_ledger():
    """Return prior PRIMARY_METRIC records (JSONL), oldest first. Each record:
    {ts, cycle, version, metric}. Empty list if absent."""
    path = os.path.join(ORCHESTRATOR_DIR, METRIC_LEDGER_FILENAME)
    records = []
    if not os.path.isfile(path):
        return records
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log(f"Could not read {METRIC_LEDGER_FILENAME}: {e}", "WARN")
    return records


def append_metric_ledger(cycle_num, version_name, metric):
    """Append one completed cycle's evaluator-declared PRIMARY_METRIC."""
    path = os.path.join(ORCHESTRATOR_DIR, METRIC_LEDGER_FILENAME)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cycle": cycle_num,
        "version": version_name,
        "metric": metric,
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"Could not append to {METRIC_LEDGER_FILENAME}: {e}", "WARN")


def noimprove_streak_from_ledger(records):
    """Given the metric ledger (oldest first), return (streak, best_so_far).

    `streak` = number of the most-recent consecutive records that did NOT set a
    new all-time best. `best_so_far` = the max metric ever recorded (None if the
    ledger is empty). A record that ties the best does NOT count as improvement
    (a tie is not progress toward the goal). Task-agnostic: operates purely on
    the declared scalar, higher-is-better."""
    best = None
    streak = 0
    for r in records:
        m = r.get("metric")
        if m is None:
            continue
        if best is None or m > best:
            best = m
            streak = 0            # new best → progress → reset
        else:
            streak += 1           # no new best → no progress
    return streak, best


def _find_nested_symlink(root):
    """Return the first symlink under root, including symlinked directories."""
    if os.path.islink(root):
        return root
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            candidate = os.path.join(current, name)
            if os.path.islink(candidate):
                return candidate
    return None


def _copytree_without_symlinks(source, target):
    link = _find_nested_symlink(source)
    if link:
        raise OSError(f"source contains symlink: {link}")
    return shutil.copytree(source, target)


def setup_v1(base_dir, codebase_path):
    """
    If the first version dir doesn't exist, copy codebase_path → base_dir/V1_baseline.
    `base_dir` is the WorkSpace/ dir. Returns the absolute path to V1_baseline/.
    """
    os.makedirs(base_dir, exist_ok=True)
    v1_path = os.path.join(base_dir, FIRST_VERSION_NAME)
    if os.path.lexists(v1_path):
        link = _find_nested_symlink(v1_path) if os.path.isdir(v1_path) else v1_path
        if link:
            log(f"Refusing invalid existing {FIRST_VERSION_NAME}/ containing symlink: {link}", "WARN")
            return None
        log(f"{FIRST_VERSION_NAME}/ already exists at {v1_path}")
        return v1_path

    if not codebase_path or os.path.islink(codebase_path) or not os.path.isdir(codebase_path):
        log(f"Cannot create {FIRST_VERSION_NAME}/ — codebase_path is invalid or a symlink", "WARN")
        return None

    log(f"Copying codebase to {WORKSPACE_DIRNAME}/{FIRST_VERSION_NAME}/: {codebase_path} → {v1_path}")
    try:
        _copytree_without_symlinks(codebase_path, v1_path)
    except OSError as e:
        if os.path.isdir(v1_path) and not os.path.islink(v1_path):
            shutil.rmtree(v1_path, ignore_errors=True)
        log(f"Cannot create {FIRST_VERSION_NAME}/ safely: {e}", "WARN")
        return None
    log(f"{FIRST_VERSION_NAME}/ created successfully ({v1_path})", "SUCCESS")
    return v1_path


def get_latest_version_dir(base_dir):
    """Return the path to the highest-numbered version directory, or None."""
    versions = scan_versions(base_dir)
    if versions:
        return versions[-1]["path"]
    return None


# ---------------------------------------------------------------------------
# Cycle runner
# ---------------------------------------------------------------------------

def append_research_log(cwd, summary, cycle_num, success):
    """Append the orchestrator agent's cycle summary to research_log.md."""
    if not cwd:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "SUCCESS" if success else "CONTINUE"
    entry = (
        f"\n## Cycle {cycle_num} — {ts} — {status}\n\n"
        f"{summary.strip()}\n"
    )
    log_path = os.path.join(cwd, RESEARCH_LOG)
    try:
        with open(log_path, "a") as f:
            f.write(entry)
    except OSError as e:
        log(f"Could not append to research_log.md: {e}", "WARN")


def maybe_create_new_version(planner_output, cwd, base_dir):
    """Create a fresh validated version or return ``(None, reason)``."""
    if not base_dir:
        return None, "versioning is disabled"
    version_info = parse_version_directive(planner_output)
    if not version_info:
        return None, "missing or invalid VERSION directive"
    new_name, from_name = version_info
    root = os.path.realpath(base_dir)
    from_path = os.path.join(root, from_name)
    new_path = os.path.join(root, new_name)
    if os.path.commonpath((root, os.path.realpath(from_path))) != root:
        return None, "source escapes WorkSpace"
    if os.path.commonpath((root, os.path.realpath(new_path))) != root:
        return None, "target escapes WorkSpace"
    if os.path.islink(from_path) or not os.path.isdir(from_path):
        return None, "source is missing, invalid, or a symlink"
    if os.path.lexists(new_path):
        return None, "target already exists"
    try:
        _copytree_without_symlinks(from_path, new_path)
    except OSError as e:
        if os.path.isdir(new_path) and not os.path.islink(new_path):
            shutil.rmtree(new_path, ignore_errors=True)
        return None, f"copy failed: {e}"
    log(f"Created new version: {new_name} (copied from {from_name})")
    return new_path, ""


def run_cycle_v2(cycle_num, config, base_dir, current_version_dir,
                 agent_timeout=3600, goal_path=None, goal_text=None,
                 max_turns=DEFAULT_MAX_CYCLE_TURNS, result_baseline_callback=None,
                 replan_diagnostic=None):
    """
    Fixed-order cycle: planner -> executer -> secretary -> verifier -> evaluator.

    The orchestrator agent sits between each worker as a relay/prompt-formatter.
    It reads the previous worker's output and formulates the prompt for the next
    worker (whose identity is determined by the harness, not the orchestrator).
    The orchestrator can bail early via end_cycle/abort but cannot choose which
    worker to call — the harness enforces the CYCLE_STAGES order.

    Returns (CycleResult, new_version_dir).
    """
    cwd = (current_version_dir
           or config.codebase_path
           or base_dir
           or ORCHESTRATOR_DIR)
    version_name = os.path.basename(cwd)

    log(f"{'=' * 50}")
    log(f"Starting Cycle {cycle_num} (fixed-order) — version: {version_name}")
    log(f"{'=' * 50}")

    session = CycleSession(
        cycle_num=cycle_num,
        orchestrator_session_id=str(uuid.uuid4()),
    )
    if replan_diagnostic:
        session.last_worker_role = "_replan_diagnostic"
        session.last_worker_output = (
            "[ERROR_REPLAN DIAGNOSTIC]\n"
            f"{replan_diagnostic}\n"
            "The Planner must correct this specific failure in the next plan."
        )
    record_session_uuid(
        ORCHESTRATOR_ROLE,
        session.orchestrator_session_id,
        "cycle_start",
        cycle=cycle_num,
    )
    log(f"Cycle {cycle_num} — orchestrator session UUID: {session.orchestrator_session_id[:8]}…")

    while session.stage_index < len(CYCLE_STAGES) and not session.cycle_done:
        expected_worker = CYCLE_STAGES[session.stage_index]

        if session.turns_used >= max_turns:
            break

        # --- Ask orchestrator for the prompt to send to expected_worker ---
        schema_retries = 0
        pending_synthetic = None
        reply = None
        while schema_retries < 3:
            if pending_synthetic is not None:
                session.last_worker_role = "_orchestrator_error"
                session.last_worker_output = pending_synthetic
                pending_synthetic = None

            reply = run_orchestrator_agent_turn(
                session, goal_text or "", agent_timeout, max_turns
            )
            session.turns_used += 1

            if "_error" in reply:
                log(f"Cycle {cycle_num} — orchestrator turn {session.turns_used}: "
                    f"schema violation: {reply['_error']}", "WARN")
                pending_synthetic = (
                    f"[ERROR] schema violation: {reply['_error']}\n"
                    f"Your raw reply was:\n{reply.get('_raw', '(empty)')}\n"
                    f"Reply again with valid JSON. Expected target: {expected_worker}"
                )
                session.orchestrator_history.append({
                    "turn": session.turns_used,
                    "reply": {"_error": reply["_error"]},
                })
                schema_retries += 1
                continue
            break
        else:
            log(f"Cycle {cycle_num} — orchestrator failed 3 schema retries, force-ending", "ERROR")
            session.cycle_done = True
            session.success = False
            append_research_log(cwd, f"Cycle force-ended: orchestrator schema failures.", cycle_num, False)
            break

        action = reply["action"]
        target = reply.get("target")
        log(f"Cycle {cycle_num} — orchestrator turn {session.turns_used}: "
            f"action={action} target={target} cycle_done={reply['cycle_done']} "
            f"success={reply['success']}")
        session.orchestrator_history.append({
            "turn": session.turns_used,
            "reply": {k: v for k, v in reply.items() if k != "prompt"},
        })

        # --- Handle abort / end_cycle (orchestrator can bail at any stage) ---
        if action == "abort":
            log(f"Cycle {cycle_num} — orchestrator ABORTED: {reply.get('reason', '')}", "ERROR")
            return CycleResult("ABORT", reply.get("reason", "orchestrator abort")), cwd

        if action == "end_cycle":
            session.cycle_done = True
            # The orchestrator does NOT decide success. The ONLY thing that makes
            # a cycle a SUCCESS stop is the evaluator having emitted a verified
            # `EVAL_VERDICT: GOAL_MET` (recorded in session.eval_goal_met). We
            # ignore reply["success"] entirely — an over-eager orchestrator
            # cannot end the whole project on a GOAL_NOT_MET / INCONCLUSIVE cycle.
            session.success = session.eval_goal_met
            append_research_log(cwd, reply.get("summary", ""), cycle_num, session.success)
            break

        # --- Enforce fixed routing: override target if orchestrator got it wrong ---
        if target != expected_worker:
            log(f"Cycle {cycle_num} — orchestrator said target={target}, "
                f"overriding to {expected_worker} (fixed order)", "WARN")
            target = expected_worker

        worker_role = target
        worker_prompt = reply["prompt"]
        if worker_role == "planner" and replan_diagnostic:
            worker_prompt = (
                "[ERROR_REPLAN DIAGNOSTIC FROM THE PREVIOUS ATTEMPT]\n"
                f"{replan_diagnostic}\n"
                "Your new plan must explicitly correct this failure.\n\n"
                f"{worker_prompt}"
            )
        log(f"Cycle {cycle_num} — calling {worker_role} ({len(worker_prompt)} char prompt)")
        worker_out = call_agent(worker_role, worker_prompt,
                                timeout=agent_timeout, cwd=cwd)

        # --- Secretary is non-critical: skip on ANY error ---
        if worker_role == "secretary" and is_agent_error(worker_out):
            if "already in use" in worker_out:
                log(f"Secretary session in use (user is chatting) — skipping", "INFO")
            else:
                log(f"Secretary failed ({worker_out[:120]}…) — skipping (non-critical)", "WARN")
            session.last_worker_role = "secretary"
            session.last_worker_output = "[SECRETARY SKIPPED] Proceed to verifier."
            session.stage_index += 1
            continue

        # --- Worker failure: retry once, then end_cycle ---
        if is_agent_error(worker_out):
            first_worker_error = worker_out
            log(f"Cycle {cycle_num} — worker {worker_role} failed, retrying once", "WARN")
            worker_out_retry = call_agent(worker_role, worker_prompt,
                                          timeout=agent_timeout, cwd=cwd)
            if is_agent_error(worker_out_retry):
                diagnostic = (
                    f"{worker_role.capitalize()} subprocess failed twice. "
                    f"First failure: {first_worker_error[:1000]}\n"
                    f"Retry failure: {worker_out_retry[:1000]}"
                )
                log(f"Cycle {cycle_num} — worker {worker_role} failed twice, ending cycle", "ERROR")
                append_research_log(
                    cwd,
                    f"Cycle ended: {diagnostic}",
                    cycle_num, False,
                )
                if worker_role == "verifier":
                    return CycleResult("ERROR_REPLAN", diagnostic), cwd
                session.cycle_done = True
                session.success = False
                break
            worker_out = worker_out_retry

        # --- Process successful worker output ---
        session.last_worker_role = worker_role
        session.last_worker_output = worker_out

        if worker_role == "verifier":
            verifier_status, phen, verifier_error = validate_verifier_terminal(worker_out)
            if verifier_error:
                diagnostic = (
                    f"Verifier terminal contract was invalid: {verifier_error}. "
                    f"Raw verifier output: {(worker_out or '(empty response)').strip()[:2000]}"
                )
                return CycleResult("ERROR_REPLAN", diagnostic), cwd
            known_failures = {"KILLED_BUDGET", "KILLED_EARLY_STOP", "CRASHED", "ERROR"}
            if verifier_status in known_failures:
                diagnostic = _verifier_replan_diagnostic(verifier_status, worker_out, phen)
                log(f"Cycle {cycle_num} — verifier STATUS: {verifier_status}, replanning")
                append_research_log(cwd, diagnostic, cycle_num, False)
                return CycleResult("ERROR_REPLAN", diagnostic), cwd
            _save_last_phenomena(phen)

        elif worker_role == "evaluator":
            ok, reason = _validate_eval_verdict_goal_met(worker_out, cwd)
            # This is the SINGLE authoritative success signal for the whole run.
            # eval_goal_met is True iff the evaluator literally emitted
            # `EVAL_VERDICT: GOAL_MET` AND that claim passed the evidence-path
            # check. GOAL_NOT_MET / INCONCLUSIVE / a failed evidence check all
            # leave it False. Nothing else (orchestrator, verifier, secretary)
            # can ever set it.
            verdict = parse_evaluator_verdict(worker_out) or "UNKNOWN"
            session.eval_goal_met = verdict == "GOAL_MET" and ok
            sig = parse_change_signature(session.planner_output or "") or {}
            append_knowledge_digest(
                f"OUTCOME (cycle {cycle_num}, evaluator)",
                f"EVAL_VERDICT={verdict}; evidence_valid={ok}; axis='{sig.get('axis','')}' "
                f"delta='{sig.get('delta','')}' tier='{sig.get('tier','')}'"
            )
            # Capture non-binding INSIGHT / AXIS_CLOSED lines the evaluator may
            # emit into durable memory, so a negative result ("this axis is flat")
            # becomes first-class knowledge instead of vanishing into GOAL_NOT_MET.
            for insight in re.findall(r"INSIGHT:\s*(.+)", worker_out or "", re.IGNORECASE):
                append_knowledge_digest(f"INSIGHT (cycle {cycle_num}, evaluator)", insight.strip())
            for closure in _extract_axis_closures(worker_out or ""):
                append_knowledge_digest(f"AXIS_CLOSED (cycle {cycle_num}, evaluator)", closure)
            # Record the evaluator-declared PRIMARY_METRIC (if any) to the metric
            # ledger — the task-agnostic input to the no-improvement guard. We only
            # ever store the number the evaluator chose to declare; the harness
            # never parses the task's own results files.
            primary_metric = parse_primary_metric(worker_out or "")
            if primary_metric is not None:
                append_metric_ledger(cycle_num, os.path.basename(cwd), primary_metric)
                append_knowledge_digest(
                    f"METRIC (cycle {cycle_num}, evaluator)",
                    f"PRIMARY_METRIC={primary_metric} (higher-is-better; version "
                    f"{os.path.basename(cwd)})"
                )
            if not ok:
                log(f"[VALIDATION] Evaluator GOAL_MET reply failed evidence check: {reason}", "WARN")
                session.last_worker_output = (
                    f"{worker_out}\n\n"
                    f"---\n"
                    f"[HARNESS VALIDATION WARNING] {reason}\n"
                    f"The evaluator's GOAL_MET claim is suspect — cited paths do not exist on disk. "
                    f"Do NOT set success=true until the evidence is verified.\n"
                )

        elif worker_role == "planner":
            if session.planner_output is None:
                # --- Plan-dedup gate (project-agnostic anti-overfit backstop) ---
                # Key only on the planner's own ## Change Signature, never on
                # parsed domain hyperparameters, so this works for any project.
                # A refine-tier plan that re-samples an already-exhausted axis is
                # bounced back with feedback (up to MAX_DEDUP_REPROMPTS times);
                # transplant/novel tiers are always allowed through.
                ledger = load_plan_ledger()
                while RunConfig.MAX_DEDUP_REPROMPTS > 0 and \
                        session.dedup_reprompts < RunConfig.MAX_DEDUP_REPROMPTS:
                    sig = parse_change_signature(worker_out)
                    if not sig:
                        break  # no signature to judge — let it through
                    is_dup, reason = check_plan_duplicate(sig, ledger)
                    if not is_dup:
                        break
                    session.dedup_reprompts += 1
                    log(f"Cycle {cycle_num} — plan-dedup gate rejected plan "
                        f"(reprompt {session.dedup_reprompts}/{RunConfig.MAX_DEDUP_REPROMPTS}): {reason}",
                        "WARN")
                    dedup_prompt = (
                        f"[HARNESS PLAN-DEDUP REJECTION] Your proposed plan was rejected as a "
                        f"low-value duplicate: {reason}.\n\n"
                        f"This is the anti-overfit backstop. Do NOT re-submit the same axis at "
                        f"another value. Propose a genuinely different next step: a structural "
                        f"change / method prior from goal.md's ## Research Strategy, a "
                        f"literature-grounded idea (raise ## Change Signature Tier to "
                        f"'transplant' or 'novel'), or a different axis entirely. Re-emit your "
                        f"FULL plan in the required format (## Version, ## Change Signature, "
                        f"## Cycle Step Budget, ## Analysis, ## Knowledge Basis, ## Hypothesis, "
                        f"## Plan, ## Expected Outcome, ## Risk Assessment)."
                    )
                    worker_out_re = call_agent(worker_role, dedup_prompt,
                                               timeout=agent_timeout, cwd=cwd)
                    if is_agent_error(worker_out_re):
                        log(f"Cycle {cycle_num} — planner dedup-reprompt failed at subprocess "
                            f"level; keeping original plan", "WARN")
                        break
                    worker_out = worker_out_re
                    session.last_worker_output = worker_out

                session.planner_output = worker_out
                new_cwd, version_error = maybe_create_new_version(worker_out, cwd, base_dir)
                if not new_cwd:
                    log(f"Planner VERSION rejected: {version_error}", "ERROR")
                    return CycleResult("ERROR_REPLAN", version_error), cwd
                cwd = new_cwd
                version_name = os.path.basename(cwd)
                log(f"Switched to version directory: {version_name}")
                if result_baseline_callback is not None:
                    result_baseline_callback(result_signatures(base_dir))
                # --- Record the accepted plan to durable memory ---
                sig = parse_change_signature(worker_out)
                if sig:
                    append_plan_ledger(cycle_num, os.path.basename(cwd), sig)
                    if sig.get("axis") or sig.get("delta"):
                        append_knowledge_digest(
                            f"TRIED (cycle {cycle_num}, {sig.get('tier','?')})",
                            f"axis='{sig.get('axis','')}' delta='{sig.get('delta','')}'. "
                            f"Outcome will be recorded as a later OUTCOME entry for the same cycle."
                        )
                for closure in _extract_axis_closures(worker_out):
                    append_knowledge_digest(f"AXIS_CLOSED (cycle {cycle_num})", closure)

        session.stage_index += 1

    # --- After all stages: one final orchestrator call for end_cycle ---
    if not session.cycle_done and session.turns_used < max_turns:
        session.stage_index = len(CYCLE_STAGES)
        reply = run_orchestrator_agent_turn(
            session, goal_text or "", agent_timeout, max_turns
        )
        session.turns_used += 1
        if "_error" not in reply:
            session.orchestrator_history.append({
                "turn": session.turns_used,
                "reply": {k: v for k, v in reply.items() if k != "prompt"},
            })
            session.cycle_done = True
            # Success is owned by the evaluator verdict alone — never by the
            # orchestrator's reply["success"].
            session.success = session.eval_goal_met
            append_research_log(cwd, reply.get("summary", ""), cycle_num, session.success)
        else:
            session.cycle_done = True
            session.success = session.eval_goal_met
            append_research_log(
                cwd,
                f"Cycle ended after all stages. Auto-detected success={session.success}.",
                cycle_num, session.success,
            )

    if session.turns_used >= max_turns and not session.cycle_done:
        log(f"Cycle {cycle_num} exceeded max_turns={max_turns} — force-ending", "WARN")
        session.cycle_done = True
        session.success = False
        append_research_log(
            cwd,
            f"Cycle force-ended after {max_turns} orchestrator turns without end_cycle.",
            cycle_num, False,
        )

    if session.success:
        return CycleResult("SUCCESS", ""), cwd
    return CycleResult("COMPLETED", ""), cwd


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(description="Run the Easy-Auto-Research for Deep Learning harness.")
    parser.add_argument("--goal", default=RunConfig.GOAL_PATH or os.path.join(ORCHESTRATOR_DIR, "goal.md"))
    parser.add_argument("--pause", type=float, default=RunConfig.PAUSE)
    parser.add_argument("--max-cycles", type=int, default=RunConfig.MAX_CYCLES)
    parser.add_argument("--max-stall-cycles", type=int, default=RunConfig.MAX_STALL_CYCLES)
    parser.add_argument("--max-noimprove-cycles", type=int, default=RunConfig.MAX_NOIMPROVE_CYCLES)
    parser.add_argument("--agent-timeout", type=int, default=RunConfig.AGENT_TIMEOUT)
    parser.add_argument("--max-replans", type=int, default=RunConfig.MAX_REPLANS)
    parser.add_argument("--max-dedup-reprompts", type=int, default=RunConfig.MAX_DEDUP_REPROMPTS)
    parser.add_argument("--max-cycle-turns", type=int, default=RunConfig.MAX_CYCLE_TURNS)
    parser.add_argument("--model", default=RunConfig.MODEL)
    parser.add_argument(
        "--dangerously-skip-agent-permissions", action="store_true",
        help="Pass Claude Code's permission-bypass flag; use only in a trusted isolated environment.",
    )
    return parser


def main(argv=None):
    global _running, SKIP_AGENT_PERMISSIONS
    args = build_arg_parser().parse_args(argv)
    SKIP_AGENT_PERMISSIONS = args.dangerously_skip_agent_permissions
    RunConfig.MAX_DEDUP_REPROMPTS = args.max_dedup_reprompts
    log("Run config: "
        f"pause={args.pause}s, max_cycles={args.max_cycles or '∞'}, "
        f"max_stall_cycles={args.max_stall_cycles or 'off'}, "
        f"max_noimprove_cycles={args.max_noimprove_cycles or 'off'}, "
        f"agent_timeout={args.agent_timeout}s, max_replans={args.max_replans}, "
        f"goal={args.goal}")

    def _kill_all_training_pids():
        """Find and kill all background training processes tracked by .training_pid
        files in any version directory. Best-effort; never raises."""
        ws = workspace_dir_for(goal_path)
        patterns = [
            os.path.join(ws, VERSION_GLOB, ".training_pid*"),  # WorkSpace/V*/
        ]
        seen = set()
        for pattern in patterns:
            for pid_file in glob.glob(pattern):
                if pid_file in seen:
                    continue
                seen.add(pid_file)
                try:
                    with open(pid_file) as f:
                        receipt = json.load(f)
                    if signal_pid_receipt(receipt, ws, signal.SIGINT):
                        log(f"  Interrupted verified training PID {receipt['pid']} (from {pid_file})")
                    else:
                        log(f"  Refusing unverified or legacy PID receipt: {pid_file}", "WARN")
                except (json.JSONDecodeError, OSError, ProcessLookupError):
                    pass

    def handle_sigint(sig, frame):
        global _running
        log("\nReceived SIGINT — killing everything and exiting...", "WARN")
        _running = False
        if _current_proc is not None:
            log("  Killing current agent subprocess...")
            _kill_proc(_current_proc)
        _kill_all_training_pids()
        log("  All processes killed. Exiting.", "WARN")
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)

    goal_path = os.path.abspath(args.goal)

    project_dir_for_model = os.path.dirname(goal_path)
    if shutil.which(CLAUDE_BIN) is None:
        log("Claude Code executable 'claude' not found on PATH.", "ERROR")
        sys.exit(1)
    log("  Using Claude Code")

    # ---- Resolve Claude model (priority: --model > .ar_model > None) ----
    # Must be set BEFORE parse_goal_config() so that call also uses the chosen model.
    global SELECTED_MODEL
    if args.model:
        SELECTED_MODEL = args.model
        model_source = "command-line flag"
    else:
        file_model = _read_project_model(project_dir_for_model)
        if file_model:
            SELECTED_MODEL = file_model
            model_source = f"{MODEL_CONFIG_FILENAME}"
        else:
            SELECTED_MODEL = None
            model_source = "Claude Code default (no --model passed)"
    log(f"  Using Claude model: {SELECTED_MODEL or '(default)'} [source: {model_source}]")

    # ---- Parse goal config ----
    log(f"Parsing goal config from {goal_path}...")
    config = parse_goal_config(goal_path)

    log(f"  Monitor interval: {config.monitor_interval_seconds}s")
    log(f"  Max run time: {config.max_run_time}")
    log(f"  Codebase path: {config.codebase_path or '(none — using orchestrator dir)'}")

    # Resolve agents directory: project dir first, fallback to orchestrator dir
    global AGENTS_DIR
    project_dir = os.path.dirname(goal_path)
    project_agents = os.path.join(project_dir, "agents")
    orchestrator_agents = os.path.join(ORCHESTRATOR_DIR, "agents")

    if os.path.isdir(project_agents) and any(
        os.path.exists(os.path.join(project_agents, f"{r}.md")) for r in ROLES
    ):
        AGENTS_DIR = project_agents
        log(f"Using project agents: {project_agents}")
    elif os.path.isdir(orchestrator_agents):
        AGENTS_DIR = orchestrator_agents
        log(f"Using orchestrator agents: {orchestrator_agents}")
    else:
        log(f"Agents directory not found in project or orchestrator dir", "ERROR")
        log("Run init.py first to generate agent prompts.", "ERROR")
        sys.exit(1)

    # The orchestrator agent prompt is mandatory — run_cycle_v2 cannot operate
    # without it. Fail fast at startup rather than crash mid-cycle.
    orch_prompt_path = os.path.join(AGENTS_DIR, f"{ORCHESTRATOR_ROLE}.md")
    if not os.path.exists(orch_prompt_path):
        log(f"Orchestrator agent prompt not found at {orch_prompt_path}", "ERROR")
        log("Run init.py to generate the orchestrator agent prompt.", "ERROR")
        sys.exit(1)

    # Load (or initialize) persistent session IDs from agents/.sessions.json.
    # This MUST happen after AGENTS_DIR is set, so the file lands next to the agent prompts.
    global AGENTS
    AGENTS = load_or_init_sessions()
    # Pre-populate _initialized for sessions that already have a JSONL on disk
    # (i.e., successfully initialized in a prior harness run). Without this, a
    # harness restart uses --session-id instead of --resume, the CLI rejects the
    # UUID as "already in use", and the recovery path needlessly mints a new
    # session — losing the agent's multi-turn conversation context.
    for role in ROLES:
        sid = AGENTS[role]
        jsonl_path = _claude_jsonl_path_for(sid)
        if os.path.isfile(jsonl_path):
            _initialized.add((role, sid))
    resumed = [r for r in ROLES if (r, AGENTS[r]) in _initialized]
    fresh = [r for r in ROLES if (r, AGENTS[r]) not in _initialized]
    log(f"Loaded internal Claude Code role sessions from {_sessions_path()}: " +
        ", ".join(f"{r}={AGENTS[r][:8]}\u2026" for r in ROLES))
    if resumed:
        log(f"  Resumable sessions (JSONL exists on disk): {', '.join(resumed)}")
    if fresh:
        log(f"  Fresh sessions (no prior JSONL): {', '.join(fresh)}")

    # Load PROJECT_BRIEF.md (shared codebase + ultimate-goal context).
    # Prepended to every agent prompt by call_agent().
    global PROJECT_BRIEF
    PROJECT_BRIEF = load_project_brief()
    if PROJECT_BRIEF:
        log(f"Loaded PROJECT_BRIEF.md ({len(PROJECT_BRIEF)} chars) — will be prepended to every agent call")
    else:
        log("PROJECT_BRIEF.md not found — agents will run without shared brief", "WARN")

    # Load group_duty.md (team role descriptions prepended to every agent prompt).
    global GROUP_DUTY
    GROUP_DUTY = load_group_duty()
    if GROUP_DUTY:
        log(f"Loaded group_duty.md ({len(GROUP_DUTY)} chars) — will be prepended to every agent call")
    else:
        log("group_duty.md not found — agents will run without team duty context", "WARN")

    # Load full goal.md text — fed to the orchestrator agent every turn so it
    # judges success against the actual termination clause, not a paraphrase.
    global GOAL_TEXT
    GOAL_TEXT = load_goal_text(goal_path)
    if GOAL_TEXT:
        log(f"Loaded goal.md ({len(GOAL_TEXT)} chars) — will be fed to orchestrator agent every turn")
    else:
        log("goal.md text empty — orchestrator agent will judge success without termination text", "WARN")

    for role in ROLES:
        agent_file = os.path.join(AGENTS_DIR, f"{role}.md")
        if not os.path.exists(agent_file):
            log(f"Missing agent prompt: {agent_file}", "ERROR")
            sys.exit(1)

    # Load per-agent constraints — prepended to every agent call so agents
    # see their Dos/Don'ts right before each task.
    global AGENT_CONSTRAINTS
    AGENT_CONSTRAINTS = load_agent_constraints()
    if AGENT_CONSTRAINTS:
        log(f"Loaded constraints for {len(AGENT_CONSTRAINTS)} agents — will prepend to every call")
    else:
        log("No agent constraints found — constraint prepend disabled", "WARN")

    # ---- Setup versioned experiment directories ----
    # Hard-coded layout: all version dirs live in a sibling WorkSpace/ dir
    # (WorkSpace/V1_baseline, WorkSpace/V2_xxx, ...), NOT in the project dir.
    # The project dir (containing goal.md) keeps only harness/agents/logs/sessions.
    base_dir = workspace_dir_for(goal_path)
    current_version_dir = None

    if config.codebase_path:
        v1_path = setup_v1(base_dir, config.codebase_path)
        if v1_path:
            current_version_dir = v1_path
            log(f"Versioning enabled. WorkSpace: {base_dir}")
        else:
            log("Versioning disabled — could not create the first version dir", "WARN")
            base_dir = None
    else:
        log("No codebase_path — versioning disabled")
        base_dir = None

    # If versions exist, start from the latest one
    if base_dir:
        latest = get_latest_version_dir(base_dir)
        if latest:
            current_version_dir = latest
            log(f"Starting from latest version: {os.path.basename(latest)}")

    # ---- Resume from research log ----
    cwd = current_version_dir or config.codebase_path
    cycle = get_last_cycle_number(cwd)
    if cycle > 0:
        log(f"Resuming from cycle {cycle} (found in {RESEARCH_LOG})")

    # ---- Main loop ----
    log(f"\n{'=' * 60}")
    log("AUTO-RESEARCH ORCHESTRATOR STARTED")
    log(f"{'=' * 60}\n")

    error_context = None
    replan_count = 0

    # Stall-guard: count consecutive cycles that produce NO new results-bearing
    # version dir. A real training cycle (even a failed / GOAL_NOT_MET one) resets
    # the streak; only pure idle/holding cycles accumulate it. Seed the baseline
    # with whatever completed-training versions already exist (e.g. on resume).
    stall_streak = 0
    seen_result_signatures = result_signatures(base_dir)

    def _refresh_cycle_result_baseline(signatures):
        nonlocal seen_result_signatures
        seen_result_signatures = signatures

    # No-improvement guard: how many consecutive completed cycles have failed to
    # set a new best PRIMARY_METRIC. Seeded from any existing metric ledger (e.g.
    # on resume) so the streak survives a relaunch. Unlike the stall-guard this
    # NEVER stops the run — at the limit it injects a mandatory arxiv directive
    # and re-arms (streak keeps climbing until a new best resets it), so a run
    # that keeps training but never improves is repeatedly pushed to seek
    # external help rather than looping on knob tweaks forever.
    noimprove_streak, best_metric = noimprove_streak_from_ledger(load_metric_ledger())
    noimprove_directive_at = -1   # last streak value we already fired a directive for

    while _running:
        cycle += 1

        if args.max_cycles > 0 and cycle > args.max_cycles:
            log(f"Reached max cycle limit ({args.max_cycles}). Stopping.")
            break

        # Re-plan limit guard. Reset the counter window, but never discard the
        # queued failure diagnostic before its next Planner turn consumes it.
        error_context, replan_count, replan_boundary = _prepare_replan_attempt(
            error_context, replan_count, args.max_replans,
        )
        if replan_boundary:
            log(
                f"Max re-plans ({args.max_replans}) reached — delivering the queued "
                "diagnostic before resetting the re-plan counter.",
                "WARN",
            )

        try:
            result, current_version_dir = run_cycle_v2(
                cycle_num=cycle,
                config=config,
                base_dir=base_dir,
                current_version_dir=current_version_dir,
                agent_timeout=args.agent_timeout,
                goal_path=goal_path,
                goal_text=GOAL_TEXT,
                max_turns=args.max_cycle_turns,
                result_baseline_callback=_refresh_cycle_result_baseline,
                replan_diagnostic=error_context,
            )
        except Exception as e:
            log(f"Unhandled exception in cycle {cycle}: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            error_context = f"Orchestrator exception: {e}"
            replan_count += 1
            continue

        if result.status == "SUCCESS":
            log(f"\n{'=' * 60}")
            log("AUTO-RESEARCH COMPLETE — SUCCESS", "SUCCESS")
            log(f"{'=' * 60}\n")
            break

        if result.status == "ABORT":
            log(f"\n{'=' * 60}")
            log(f"AUTO-RESEARCH ABORTED: {result.error_context}", "ERROR")
            log(f"{'=' * 60}\n")
            break

        elif result.status == "ERROR_REPLAN":
            error_context = result.error_context
            replan_count += 1
            log(f"Re-plan {replan_count}/{args.max_replans} queued.")
            # No pause — immediately re-plan
            continue

        else:
            # COMPLETED or FULL_TRAIN — reset re-plan counter, pause
            error_context = None
            replan_count = 0
            if result.status == "FULL_TRAIN":
                log("Full training trigger met. Next cycle should plan full training.")

            # ---- Stall-guard: stop after N consecutive idle (no-new-training) cycles ----
            if args.max_stall_cycles > 0:
                now_result_signatures = result_signatures(base_dir)
                changed_results = {
                    path for path, signature in now_result_signatures.items()
                    if seen_result_signatures.get(path) != signature
                }
                if changed_results:
                    # Only files created or changed after the cycle baseline count.
                    seen_result_signatures = now_result_signatures
                    if stall_streak:
                        log(f"Stall streak reset (new training output: "
                            f"{', '.join(sorted(changed_results))}).")
                    stall_streak = 0
                else:
                    stall_streak += 1
                    log(f"Idle cycle {cycle}: no new training output "
                        f"({stall_streak}/{args.max_stall_cycles} consecutive).", "WARN")
                    # Escalating directives BEFORE the hard stop: turn a stall into
                    # a push toward external knowledge instead of just counting down.
                    # These are auto-written into human_comments.txt (the planner's
                    # one-shot channel) and consumed on the next planner turn.
                    if stall_streak < args.max_stall_cycles:
                        if stall_streak == max(1, args.max_stall_cycles // 2):
                            _append_human_comment(
                                "[HARNESS STALL-GUARD — escalation 1] The last few cycles "
                                "produced no new training results. Do NOT propose another "
                                "small knob tweak. Consult the KNOWLEDGE DIGEST, then pick an "
                                "untried method prior from goal.md's ## Research Strategy and "
                                "plan it as a 'transplant'-tier structural change."
                            )
                            log("Stall-guard: wrote escalation-1 directive to human_comments.txt", "INFO")
                        elif stall_streak == args.max_stall_cycles - 1:
                            _append_human_comment(
                                "[HARNESS STALL-GUARD — escalation 2, final] This is the last "
                                "cycle before the run auto-stops for stalling. You MUST run "
                                "arxiv-verified-search this turn and base your next hypothesis "
                                "on a returned, code-backed paper (## Knowledge Basis: CITED), "
                                "adapting it within the authorized surface. If no external idea "
                                "is usable, say so explicitly and explain why the goal may be "
                                "infeasible under the current constraints."
                            )
                            log("Stall-guard: wrote escalation-2 (final) directive to human_comments.txt", "INFO")
                    if stall_streak >= args.max_stall_cycles:
                        log(f"\n{'=' * 60}")
                        log(f"AUTO-RESEARCH STOPPED — {stall_streak} consecutive idle "
                            f"cycles with no new training output. The research has "
                            f"stalled (goal not met and no new experiments being run). "
                            f"Review the reports and provide direction via "
                            f"human_comments.txt, then relaunch.", "WARN")
                        log(f"{'=' * 60}\n")
                        break

            # ---- No-improvement guard: push toward external help (never stops) ----
            # Everything serves metric optimization: cycles that keep producing new
            # training output but never beat the best-so-far metric are the failure
            # mode the stall-guard cannot see (stall_streak stays 0 because training
            # DID run). Re-derive the streak from the metric ledger the evaluator
            # branch just appended to, and when it reaches the limit, hard-inject a
            # mandatory arxiv-verified-search directive via human_comments.txt. This
            # never terminates the run — it re-arms so a persistently flat run is
            # repeatedly pushed to seek prior art instead of re-tuning knobs.
            if args.max_noimprove_cycles > 0:
                prev_best = best_metric
                noimprove_streak, best_metric = noimprove_streak_from_ledger(
                    load_metric_ledger())
                if best_metric is not None and (prev_best is None or best_metric > prev_best):
                    if noimprove_directive_at != -1:
                        log(f"No-improve streak reset — new best PRIMARY_METRIC "
                            f"{best_metric}.")
                    noimprove_directive_at = -1
                elif noimprove_streak >= args.max_noimprove_cycles and \
                        noimprove_streak != noimprove_directive_at:
                    # Fire once per distinct streak value so a run that stays flat
                    # for many cycles keeps getting re-nudged, but we don't spam the
                    # same directive twice for the same cycle.
                    noimprove_directive_at = noimprove_streak
                    log(f"No-improve guard: {noimprove_streak} consecutive cycles with "
                        f"no new best metric (best={best_metric}). Injecting mandatory "
                        f"arxiv-verified-search directive.", "WARN")
                    _append_human_comment(
                        f"[HARNESS NO-IMPROVE GUARD] {noimprove_streak} consecutive "
                        f"completed cycles have failed to beat the best PRIMARY_METRIC "
                        f"seen so far ({best_metric}). The current tuning path is not "
                        f"improving the metric. You MUST this turn: (1) in ## Analysis, "
                        f"summarize concretely what has been tried and why it plateaued "
                        f"(consult the KNOWLEDGE DIGEST); (2) run arxiv-verified-search "
                        f"with a query built from that specific failure mode (target the "
                        f"open problem, not the generic task); (3) base your next "
                        f"hypothesis on a returned, code-backed paper and cite it in "
                        f"## Knowledge Basis (Tier 'transplant' or 'novel'). Do NOT "
                        f"propose another same-axis knob tweak."
                    )

        # Pause between normal cycles
        if _running:
            log(f"Pausing {args.pause}s before next cycle...")
            interruptible_sleep(args.pause)

    # ---- Final summary ----
    final_cwd = current_version_dir or config.codebase_path or ORCHESTRATOR_DIR
    log(f"\nOrchestrator stopped after cycle {cycle}.")
    log(f"Current version: {os.path.basename(final_cwd)}")
    if base_dir:
        versions = scan_versions(base_dir)
        log(f"Total versions: {len(versions)} ({', '.join(v['name'] for v in versions)})")
    log(f"Research log: {os.path.join(final_cwd, RESEARCH_LOG)}")
    log(f"Agent audit log: {os.path.join(final_cwd, AGENT_LOG_FILE)}")


if __name__ == "__main__":
    main()