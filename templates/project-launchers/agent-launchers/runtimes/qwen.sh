#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

if [ "${ROLE:-}" = "orch-manager" ]; then
  if ! command -v qwen >/dev/null 2>&1; then
    echo "missing qwen CLI on PATH" >&2
    exit 1
  fi
  PROMPT="$(render_prompt "$PROMPT_FILE")"
  cd "$WORKTREE"
  # Qwen Code stores MCP config separately from Claude/Codex. Keep this project-scoped
  # and idempotent enough for repeated launches by removing any stale project entry first.
  qwen mcp remove orchestrator --scope project >/dev/null 2>&1 || true
  qwen mcp add orchestrator "$ORCH/.venv/bin/python" \
    -s project \
    -t stdio \
    -e "PYTHONPATH=$ORCH" \
    -e "ORCH_INSTANCE=$PROJECT" \
    --trust \
    -- -m orchestrator.cli --instance "$PROJECT" serve >/dev/null
  exec qwen -i "$PROMPT" "$@"
fi

if [ ! -x "$QWEN_VENV/bin/python" ]; then
  echo "missing qwen-agent python: $QWEN_VENV/bin/python" >&2
  exit 1
fi
if [ ! -f "$QWEN_WORKER" ]; then
  echo "missing qwen worker: $QWEN_WORKER" >&2
  exit 1
fi

export ORCH
export PROJECT
export ORCH_INSTANCE="$PROJECT"
export PYTHONPATH="$ORCH${PYTHONPATH:+:$PYTHONPATH}"
export ORCH_DASHBOARD="$DASHBOARD"
export AGENT_SYSTEM_PROMPT="$(render_prompt "$PROMPT_FILE")"

exec "$QWEN_VENV/bin/python" "$QWEN_WORKER" --worktree "$WORKTREE" "$@"
