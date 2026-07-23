#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

# --print-cmd (durable-worker-sidecar plan §5/§D): guarded, zero-impact on
# normal runs -- ONLY consumed when it is literally the first argument, before
# anything else looks at "$@". Prints the exact exec command line instead of
# running it, so start-agent.sh's AGENT_SIDECAR branch can capture it as the
# tmux `spawn_cmd` the side-car uses for ensure_worker()/restart().
PRINT_CMD_ONLY=0
if [ "${1:-}" = "--print-cmd" ]; then
  PRINT_CMD_ONLY=1
  shift
fi

PROMPT="$(render_prompt "$PROMPT_FILE")"
cd "$WORKTREE"

LAUNCH_MODE="$(resolve_agent_mode non-interactive)"
PROMPT="$(apply_interactive_prompt "$PROMPT" "$LAUNCH_MODE")"

if [ "$PROMPT_NAME" = "orch-manager" ] && [ -z "${CLAUDE_MODEL:-}" ] \
   && [ -x "$LAUNCHER_DIR/model-settings.py" ]; then
  eval "$("$LAUNCHER_DIR/model-settings.py" orch_manager_claude)"
fi

# Wire the orchestrator MCP server for Claude Code (anthropic model/auth is
# unchanged). Generated into a temp file and passed with --strict-mcp-config so
# it does not depend on any pre-existing ~/.claude.json / .mcp.json state.
# verify_run blocks for the full monorepo suite (~35min). Give the MCP client a
# tool-call timeout above the engine's 3000s verify ceiling so a green run is
# received and gated instead of being abandoned client-side (was bouncing #43).
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-3300000}"
export MCP_TIMEOUT="${MCP_TIMEOUT:-60000}"

CLAUDE_MCP_ARGS=()
CLAUDE_MCP_FILE=""
cleanup_claude_mcp() {
  [ -n "$CLAUDE_MCP_FILE" ] && [ -f "$CLAUDE_MCP_FILE" ] && rm -f "$CLAUDE_MCP_FILE"
}
# In --print-cmd mode the printed command line REFERENCES this file by path,
# and it is meant to be run later/elsewhere (tmux respawn-pane) -- an EXIT
# trap here would delete it out from under that future exec the moment this
# script's own `exit 0` below fires (a normal `exec` never runs EXIT traps,
# but the print-cmd path's early exit does). So the file is deliberately left
# behind for the lifetime of the side-car's tmux session in that mode.
[ "$PRINT_CMD_ONLY" = "1" ] || trap cleanup_claude_mcp EXIT INT TERM
CLAUDE_MCP_FILE="$(mktemp "${TMPDIR:-/tmp}/claude-mcp-${PROJECT:-workspace}.XXXXXX.json")"
if write_mcp_json "$CLAUDE_MCP_FILE"; then
  CLAUDE_MCP_ARGS=(--mcp-config "$CLAUDE_MCP_FILE" --strict-mcp-config)
else
  rm -f "$CLAUDE_MCP_FILE"; CLAUDE_MCP_FILE=""
fi

if [ "$LAUNCH_MODE" = "interactive" ]; then
  cmd=(claude --dangerously-skip-permissions "${CLAUDE_MCP_ARGS[@]}" "$PROMPT")
else
  cmd=(claude -p "$PROMPT" --dangerously-skip-permissions "${CLAUDE_MCP_ARGS[@]}")
fi
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

if [ "$PRINT_CMD_ONLY" = "1" ]; then
  # QA fix: the printed line is meant to run LATER, elsewhere (tmux
  # respawn-pane) -- a bare "claude ..." command relies on MCP_TOOL_TIMEOUT/
  # MCP_TIMEOUT being present in whatever environment eventually execs it,
  # which is NOT guaranteed (a fresh tmux pane's shell doesn't inherit this
  # script's `export`s). Prefix with `env K=V ...` so the printed command is
  # fully self-contained -- every env var this script exports for the exec.
  PRINT_ENV_ARGS=(env "MCP_TOOL_TIMEOUT=${MCP_TOOL_TIMEOUT}" "MCP_TIMEOUT=${MCP_TIMEOUT}")
  printf '%q ' "${PRINT_ENV_ARGS[@]}" "${cmd[@]}"
  printf '\n'
  exit 0
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

# Loop by default, INCLUDING interactive: run-agent-loop keeps the agent cycling
# (re-launches the command each cycle) instead of stopping after one session.
# The loop's on/off + cadence are owned by the dashboard (loop_enabled +
# poll_interval_seconds). LOOP_AGENT=1 routes non-interactive roles through it too;
# interactive (TUI) always routes so the human's session relaunches per policy.
if [ -n "${AGENT_ID:-}" ] && { [ "$LAUNCH_MODE" = "interactive" ] || [ "${LOOP_AGENT:-0}" = "1" ]; }; then
  export ORCH_DASHBOARD="$DASHBOARD"
  export AGENT_POLL="${AGENT_POLL:-${AGENT_POLL_DEFAULT:-90}}"   # fallback cadence only
  [ "$LAUNCH_MODE" = "interactive" ] && export AGENT_LOOP_INTERACTIVE=1
  if [ "${IDLE_STOP:-0}" -gt 0 ]; then
    export AGENT_IDLE_STOP="$IDLE_STOP"
  fi
  exec "$WORKSPACE_ROOT/run-agent-loop.sh" "$AGENT_ID" "${cmd[@]}"
fi

exec "${cmd[@]}"
