#!/usr/bin/env bash
# Install the easy-auto-research skill for Claude Code.
#
# Usage:
#   ./install.sh                 # install into ~/.claude/skills
#   ./install.sh <skills_dir>    # install into an explicit skills directory
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$HERE/easy-auto-research"

if [[ ! -f "$SRC/SKILL.md" ]]; then
  echo "error: $SRC/SKILL.md not found — run this from the repo root." >&2
  exit 1
fi

DEST_DIR="${1:-$HOME/.claude/skills}"

mkdir -p "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd -P)"
DEST="$DEST_DIR/easy-auto-research"
case "$DEST" in
  "$SRC"|"$SRC"/*)
    echo "error: install destination must not be the source tree or inside it: $DEST" >&2
    exit 1
    ;;
esac
rm -rf "$DEST"
cp -r "$SRC" "$DEST"

# Never ship generated run state, logs, queues, or caches even if a local source
# checkout was used as a run project by mistake.
find "$DEST" -depth \( \
  -type d \( \
    -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o \
    -name WorkSpace -o -name CycleReport -o -name solutions -o \
    -name agents -o -name agent_history -o -name agent_interactions \
  \) -o \
  -type f \( \
    -name '*.py[co]' -o -name agent_thoughts.log -o -name research_log.md -o \
    -name uuid_ledger.jsonl -o -name .sessions.json -o \
    -name .knowledge_digest.md -o -name .plan_ledger.jsonl -o \
    -name .metric_ledger.jsonl -o -name .last_phenomena.md -o \
    -name '.training_pid*' -o -name human_comments.txt -o \
    -name human_interrupt.txt -o -name '.human_comments.txt.queue.*' -o \
    -name '.human_interrupt.txt.queue.*' -o -name goal.md -o \
    -name PROJECT_BRIEF.md -o -name research_spec.json -o \
    -name PREFLIGHT.md -o -name .ar_model \
  \) \
\) -exec rm -rf -- {} + 2>/dev/null || true

echo "installed easy-auto-research -> $DEST"
echo "restart Claude Code and confirm 'easy-auto-research' appears among its skills."
