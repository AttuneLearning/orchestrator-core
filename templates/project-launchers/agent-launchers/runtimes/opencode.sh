#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

if ! command -v opencode >/dev/null 2>&1; then
  echo "missing opencode CLI on PATH" >&2
  exit 1
fi

PROMPT="$(render_prompt "$PROMPT_FILE")"
LAUNCH_MODE="$(resolve_agent_mode non-interactive)"

# Open-source model selection. Default is glm-5.2; override per launch with
# ORCH_OPENCODE_MODEL (e.g. orch_model/deepseek-v4-pro, qwen_local/qwen-local).
OPENCODE_MODEL="${ORCH_OPENCODE_MODEL:-orch_model/glm-5.2}"
ensure_opensource_keys

OPENCODE_CONFIG_HOME="$(mktemp -d "${TMPDIR:-/tmp}/opencode-${PROJECT:-workspace}.XXXXXX")"
cleanup_opencode_config() {
  if [ -n "${OPENCODE_CONFIG_HOME:-}" ] && [ -d "$OPENCODE_CONFIG_HOME" ]; then
    rm -rf "$OPENCODE_CONFIG_HOME"
  fi
}
trap cleanup_opencode_config EXIT INT TERM

mkdir -p "$OPENCODE_CONFIG_HOME/opencode"
export XDG_CONFIG_HOME="$OPENCODE_CONFIG_HOME"
OPENCODE_CONFIG_PATH="$OPENCODE_CONFIG_HOME/opencode/opencode.jsonc"

# Shared writer: open-source provider menu + orchestrator MCP (when installed).
opencode_write_config "$OPENCODE_CONFIG_PATH" "$OPENCODE_MODEL"

if [ "$LAUNCH_MODE" = "interactive" ]; then
  cd "$WORKTREE"
  # TUI: the positional arg is the PROJECT PATH; the initial message goes via
  # --prompt (passing it positionally makes opencode lstat it as a dir).
  cmd=(opencode --model "$OPENCODE_MODEL" --prompt "$PROMPT" "$@")
else
  # `opencode run [message..]` takes the message positionally.
  cmd=(opencode run --dir "$WORKTREE" --auto --model "$OPENCODE_MODEL" "$PROMPT" "$@")
fi

status=0
if [ "${ORCH_LAUNCH_DRY_RUN:-0}" = "1" ]; then
  echo "runtime=opencode"
  echo "model=$OPENCODE_MODEL"
  echo "mcp=$([ -n "${ORCH:-}" ] && [ -n "${PROJECT:-}" ] && echo "orchestrator (--instance $PROJECT serve)" || echo '(none - not an orchestrator-installed workspace)')"
  echo "api_key_configured=$([ -n "${MODEL_ACCESS_KEY:-}" ] && echo yes || echo no)"
  echo "config_path=$OPENCODE_CONFIG_PATH"
  echo "--- config ---"
  cat "$OPENCODE_CONFIG_PATH"
  printf 'command='
  printf '%q ' "${cmd[@]}"
  printf '\n'
  cleanup_opencode_config
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
  "$WORKSPACE_ROOT/run-agent-loop.sh" "$AGENT_ID" "${cmd[@]}"
  status=$?
else
  "${cmd[@]}"
  status=$?
fi

cleanup_opencode_config
exit "$status"
