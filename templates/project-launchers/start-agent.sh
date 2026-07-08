#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$WORKSPACE_ROOT/agent-launchers"

usage() {
  cat <<EOF
usage: ./start-agent.sh [--dry-run] [--no-enable-loop] <role> [runtime] [runtime args...]

roles:
  orch-manager
  backend-dev-manager | frontend-dev-manager
  backend-dev-worker  | frontend-dev-worker
  backend-qa-worker   | frontend-qa-worker
  senior-dev

runtimes: claude, codex, qwen
EOF
}

DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-enable-loop) AGENT_ENABLE_LOOP=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

ROLE_ARG="${1:-}"
if [ -z "$ROLE_ARG" ]; then
  usage >&2
  exit 1
fi
shift

source "$LAUNCHER_DIR/orchestrator.env"
source "$LAUNCHER_DIR/roles.sh"
source "$LAUNCHER_DIR/lib.sh"

resolve_role "$ROLE_ARG"
RUNTIME="${1:-$DEFAULT_RUNTIME}"
if [ $# -gt 0 ]; then
  shift
fi

case "$RUNTIME" in
  claude|codex|qwen) ;;
  *) echo "unknown runtime: $RUNTIME (expected claude, codex, or qwen)" >&2; exit 1 ;;
esac

case "$ROLE:$RUNTIME" in
  backend-dev-manager:qwen|frontend-dev-manager:qwen)
    echo "qwen is supported for dev-worker, not dev-manager/swarm coordination" >&2
    echo "use: ./start-dev-worker.sh ${TEAM} qwen [args...] or ./start-dev-manager.sh ${TEAM} claude|codex" >&2
    exit 1
    ;;
  backend-dev-manager:claude|frontend-dev-manager:claude)
    PROMPT_NAME="dev-manager-claude"
    ;;
  backend-dev-manager:codex|frontend-dev-manager:codex)
    PROMPT_NAME="dev-manager-codex"
    ;;
esac

PROMPT_FILE="$LAUNCHER_DIR/prompts/$PROMPT_NAME.md"
ADAPTER="$LAUNCHER_DIR/runtimes/$RUNTIME.sh"

if [ ! -d "$WORKTREE" ]; then
  echo "missing worktree: $WORKTREE" >&2
  exit 1
fi
if [ ! -x "$ADAPTER" ]; then
  echo "unknown or non-executable runtime adapter: $ADAPTER" >&2
  exit 1
fi

export WORKSPACE_ROOT LAUNCHER_DIR PROJECT ORCH DASHBOARD QWEN_VENV QWEN_WORKER
export ROLE RUNTIME AGENT_ID TEAM FUNCTION GATE APP WORKTREE PROMPT_NAME PROMPT_FILE
export LOOP_AGENT IDLE_STOP COMMAND_TIMEOUT FANOUT_DEFAULT AGENT_POLL_DEFAULT AGENT_ENABLE_LOOP_DEFAULT

if [ "$DRY_RUN" = "1" ]; then
  print_launch_summary
  echo "adapter=$ADAPTER"
  echo "extra_args=$*"
  if [ "$RUNTIME" = "codex" ] || [ "$RUNTIME" = "claude" ]; then
    ORCH_LAUNCH_DRY_RUN=1 "$ADAPTER" "$@"
  fi
  exit 0
fi

enable_agent_loop
exec "$ADAPTER" "$@"
