#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

if ! command -v qwen >/dev/null 2>&1; then
  echo "missing qwen CLI on PATH" >&2
  exit 1
fi

PROMPT="$(render_prompt "$PROMPT_FILE")"
LAUNCH_MODE="$(resolve_agent_mode interactive)"

QWEN_ARGS=(--yolo)
for arg in "$@"; do
  if [ "$arg" = "--yolo" ]; then
    QWEN_ARGS=()
    break
  fi
done

cd "$WORKTREE"

# Qwen Code stores MCP config separately from Claude/Codex. Keep the project entry
# scoped and fresh so dev workers talk to the right coordinator.
qwen mcp remove orchestrator --scope project >/dev/null 2>&1 || true
qwen mcp add orchestrator "$ORCH/.venv/bin/python" \
  -s project \
  -t stdio \
  -e "PYTHONPATH=$ORCH" \
  -e "ORCH_INSTANCE=$PROJECT" \
  -e "ORCH_ROLE=$ROLE" \
  --trust \
  -- -m orchestrator.cli --instance "$PROJECT" serve >/dev/null
qwen mcp approve orchestrator >/dev/null 2>&1 || true

if [ "$LAUNCH_MODE" = "interactive" ]; then
  exec qwen "${QWEN_ARGS[@]}" -i "$PROMPT" "$@"
else
  exec qwen "${QWEN_ARGS[@]}" -p "$PROMPT" "$@"
fi
