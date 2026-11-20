#!/usr/bin/env bash
# Run the agent UX evaluation: every task prompt x every agent CLI. Agents run in parallel,
# each working through the prompts in sequence (3 sessions at a time). See
# docs/benchmarks/agent-ux-protocol.md.
#
#   scripts/agent_ux_eval.sh <out-dir> [mcp-url] [task-glob]
#
# The server must already be running (clinvar-link serve --transport unified --port 18505).
# Set SLOT=<command> to wrap each session in a shared semaphore (e.g. a flock runner);
# AGENTS="claude agy" runs a subset (e.g. when one client is out of quota).
set -uo pipefail
OUT=${1:?usage: agent_ux_eval.sh <out-dir> [mcp-url] [task-glob]}
URL=${2:-http://127.0.0.1:18505/mcp}
HERE=$(cd "$(dirname "$0")" && pwd)
TASKS=${3:-"$HERE/../docs/benchmarks/tasks/[A-B]-*.md"}
NAME=eval-clinvar-link
mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd)  # sessions cd into their own dirs
curl -sf "${URL%/mcp}/health" >/dev/null || { echo "server not reachable at $URL" >&2; exit 1; }
# agy has no per-session MCP flag: register (idempotent) in its user config.
agy mcp add "$NAME" "$URL" >/dev/null

for agent in ${AGENTS:-claude codex agy}; do
  ( for task in $TASKS; do ${SLOT:-} "$HERE/agent_ux_session.sh" "$agent" "$task" "$OUT" "$URL"; done ) &
done
wait
python3 "$HERE/agent_ux_scores.py" "$OUT"
