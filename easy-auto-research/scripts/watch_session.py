#!/usr/bin/env python3
"""Live, color-formatted viewer for a Claude Code session JSONL.

Usage:
    python3 watch_session.py <session-id>            # tail forever
    python3 watch_session.py <session-id> --once     # print existing then exit
    python3 watch_session.py <session-id> --no-tools # hide tool_use/tool_result
    python3 watch_session.py <session-id> --max 800  # truncate text per block

Reads the JSONL at:
  ~/.claude/projects/<cwd-mangled>/<session-id>.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

# ANSI colors
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GREY = "\033[90m"


def find_jsonl(session_id: str) -> str:
    matches = glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{session_id}.jsonl"
    ))
    if not matches:
        sys.exit(f"No Claude Code JSONL found for session {session_id}")
    if len(matches) > 1:
        print(f"{YELLOW}Multiple matches; using first:{RESET}", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
    return matches[0]


def fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return iso[:19]


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"{DIM}…[+{len(text)-limit} chars]{RESET}"


def render_text(text: str, max_chars: int, indent: str = "  ") -> str:
    text = truncate(text, max_chars)
    return "\n".join(indent + line for line in text.splitlines())


def render_event(e: dict, max_chars: int, show_tools: bool) -> str | None:
    t = e.get("type")
    ts = fmt_ts(e.get("timestamp", ""))
    msg = e.get("message", {}) if isinstance(e.get("message"), dict) else {}

    if t == "user":
        content = msg.get("content")
        # tool_result block
        if isinstance(content, list):
            blocks = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    if not show_tools:
                        return None
                    raw = b.get("content")
                    if isinstance(raw, list):
                        text = "".join(
                            x.get("text", "") for x in raw
                            if isinstance(x, dict) and x.get("type") == "text"
                        )
                    else:
                        text = str(raw or "")
                    is_err = b.get("is_error")
                    color = RED if is_err else GREEN
                    head = f"{color}[{ts}] ◀ TOOL RESULT{RESET}"
                    if is_err:
                        head += f" {RED}(ERROR){RESET}"
                    blocks.append(head + "\n" + render_text(text, max_chars))
            return "\n".join(blocks) if blocks else None
        # plain user prompt (sent by harness.py)
        text = str(content or "")
        return (
            f"{BOLD}{BLUE}[{ts}] ▶ USER PROMPT{RESET}\n"
            + render_text(text, max_chars)
        )

    if t == "assistant":
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        blocks = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text = b.get("text", "")
                blocks.append(
                    f"{BOLD}{MAGENTA}[{ts}] ◀ ASSISTANT{RESET}\n"
                    + render_text(text, max_chars)
                )
            elif bt == "tool_use":
                if not show_tools:
                    continue
                name = b.get("name", "?")
                inp = b.get("input", {})
                inp_str = json.dumps(inp, ensure_ascii=False, indent=2) \
                    if isinstance(inp, (dict, list)) else str(inp)
                blocks.append(
                    f"{CYAN}[{ts}] ▶ TOOL CALL: {BOLD}{name}{RESET}\n"
                    + render_text(inp_str, max_chars)
                )
            elif bt == "thinking":
                text = b.get("thinking", "")
                blocks.append(
                    f"{GREY}[{ts}] ◌ THINKING{RESET}\n"
                    + render_text(text, max_chars)
                )
        return "\n".join(blocks) if blocks else None

    if t == "system":
        return None  # noisy bookkeeping
    if t in ("queue-operation", "summary"):
        return None
    return None


def stream_file(path: str, follow: bool, max_chars: int, show_tools: bool) -> None:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        # Print everything that already exists
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            out = render_event(e, max_chars, show_tools)
            if out:
                print(out, flush=True)
                print(GREY + "─" * 80 + RESET, flush=True)
        if not follow:
            return
        # Tail new lines as they arrive
        print(f"\n{DIM}— end of file; tailing for new turns (Ctrl-C to stop) —{RESET}\n",
              flush=True)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            out = render_event(e, max_chars, show_tools)
            if out:
                print(out, flush=True)
                print(GREY + "─" * 80 + RESET, flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="Pretty live viewer for a Claude Code session JSONL."
    )
    ap.add_argument("session_id", help="UUID of the agent session to watch")
    ap.add_argument("--once", action="store_true",
                    help="Print existing turns and exit (no tail)")
    ap.add_argument("--no-tools", action="store_true",
                    help="Hide tool_use and tool_result blocks")
    ap.add_argument("--max", type=int, default=2000,
                    help="Max chars per block (default 2000; 0 = unlimited)")
    args = ap.parse_args()

    path = find_jsonl(args.session_id)
    print(f"{DIM}Watching {path}{RESET}\n", file=sys.stderr)
    try:
        stream_file(path, follow=not args.once,
                    max_chars=args.max, show_tools=not args.no_tools)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped.{RESET}", file=sys.stderr)


if __name__ == "__main__":
    main()