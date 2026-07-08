#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

PROMPT="$(render_prompt "$PROMPT_FILE")"
cd "$WORKTREE"

if [ "$PROMPT_NAME" = "orch-manager" ] && [ -z "${CLAUDE_MODEL:-}" ] \
   && [ -x "$LAUNCHER_DIR/model-settings.py" ]; then
  eval "$("$LAUNCHER_DIR/model-settings.py" orch_manager_claude)"
fi

cmd=(claude -p "$PROMPT" --dangerously-skip-permissions)
if [ -n "${CLAUDE_MODEL:-}" ]; then
  cmd+=(--model "$CLAUDE_MODEL")
elif [ -n "${ORCH_CLAUDE_MODEL:-}" ]; then
  cmd+=(--model "$ORCH_CLAUDE_MODEL")
elif [ "$PROMPT_NAME" = "dev-manager" ] || [ "$PROMPT_NAME" = "orch-manager" ]; then
  cmd+=(--model opus)
fi
if [ "$PROMPT_NAME" = "dev-manager" ] || [ "$PROMPT_NAME" = "orch-manager" ]; then
  cmd+=(--verbose)
fi

if [ "${COMMAND_TIMEOUT:-0}" -gt 0 ]; then
  cmd=(timeout --signal=TERM "$COMMAND_TIMEOUT" "${cmd[@]}")
fi

if [ "${ORCH_LAUNCH_DRY_RUN:-0}" = "1" ]; then
  if [ -x "$LAUNCHER_DIR/model-settings.py" ]; then
    "$LAUNCHER_DIR/model-settings.py" orch_manager_claude --diagnostic
  fi
  printf 'command='
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

if [ "${LOOP_AGENT:-0}" = "1" ] && [ -n "${AGENT_ID:-}" ]; then
  export ORCH_DASHBOARD="$DASHBOARD"
  export AGENT_POLL="${AGENT_POLL:-${AGENT_POLL_DEFAULT:-90}}"
  if [ "${IDLE_STOP:-0}" -gt 0 ]; then
    export AGENT_IDLE_STOP="$IDLE_STOP"
  fi
  exec "$WORKSPACE_ROOT/run-agent-loop.sh" "$AGENT_ID" "${cmd[@]}"
fi

exec "${cmd[@]}"
