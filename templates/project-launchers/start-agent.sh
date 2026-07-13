#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$WORKSPACE_ROOT/agent-launchers"

usage() {
  cat <<EOF
usage: ./start-agent.sh [--dry-run] [--no-enable-loop] [--interactive|--non-interactive] [-m MODEL] <role> [runtime] [runtime args...]

roles:
  orch-manager
  backend-dev-manager | frontend-dev-manager
  backend-dev-worker  | frontend-dev-worker
  backend-qa-worker   | frontend-qa-worker
  senior-dev          | senior-qa

runtimes: claude, codex, opencode, qwen, qwen-code

model (-m/--model): shortcut or model string valid for the chosen runtime.
  omit for the runtime default (claude->opus, codex->gpt-5.4-mini, opencode->glm-5.2)
  list valid combos: agent-launchers/resolve-model.py --list

mode:
  default behavior is runtime-specific and backward compatible
  use --interactive or --non-interactive to override the launcher mode
EOF
}

DRY_RUN=0
MODEL_SEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-enable-loop) AGENT_ENABLE_LOOP=0; shift ;;
    --interactive) AGENT_MODE=interactive; shift ;;
    --non-interactive) AGENT_MODE=non-interactive; shift ;;
    -m|--model) MODEL_SEL="${2:?-m/--model requires a value}"; shift 2 ;;
    --model=*) MODEL_SEL="${1#--model=}"; shift ;;
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
  claude|codex|opencode|qwen|qwen-code) ;;
  *) echo "unknown runtime: $RUNTIME (expected claude, codex, opencode, qwen, or qwen-code)" >&2; exit 1 ;;
esac

# Resolve -m/--model against agent-model.yaml and route to the harness-specific
# env var. No -m = each harness's default (claude->opus, codex->gpt-5.4-mini,
# opencode->glm-5.2).
if [ -n "$MODEL_SEL" ]; then
  if ! RESOLVED_MODEL="$("$ORCH/.venv/bin/python" "$LAUNCHER_DIR/resolve-model.py" "$RUNTIME" "$MODEL_SEL")"; then
    echo "model '$MODEL_SEL' is not valid for runtime '$RUNTIME'." >&2
    echo "valid options:" >&2
    "$ORCH/.venv/bin/python" "$LAUNCHER_DIR/resolve-model.py" "$RUNTIME" --list >&2 || true
    exit 2
  fi
  case "$RUNTIME" in
    opencode) export ORCH_OPENCODE_MODEL="$RESOLVED_MODEL" ;;
    claude)   export CLAUDE_MODEL="$RESOLVED_MODEL" ;;
    codex)    export ORCH_CODEX_MODEL="$RESOLVED_MODEL" ;;
    *) echo "-m/--model is not supported for runtime '$RUNTIME'" >&2; exit 2 ;;
  esac
fi

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
export AGENT_MODE

if [ "$DRY_RUN" = "1" ]; then
  print_launch_summary
  echo "model_selection=${MODEL_SEL:-'(default)'}"
  echo "adapter=$ADAPTER"
  echo "extra_args=$*"
  if [ "$RUNTIME" = "codex" ] || [ "$RUNTIME" = "claude" ] || [ "$RUNTIME" = "opencode" ]; then
    ORCH_LAUNCH_DRY_RUN=1 "$ADAPTER" "$@"
  fi
  exit 0
fi

# SoT sync: re-render this worktree's CLAUDE.md/AGENTS.md/QWEN.md from the
# orchestrator's accepted ADRs at every launch, so a worker can never boot on a
# stale rules snapshot. Best-effort: an unreachable coordinator must not block
# the launch (the live adr_for_issue MCP path still delivers current rules).
DOC_FN="$FUNCTION"
[ "$DOC_FN" = "dev-manager" ] && DOC_FN="dev"   # managers share the dev worktree/rules
case "$DOC_FN" in
  dev|qa|lead)
    if [ -n "$AGENT_ID" ]; then
      timeout 30 env PYTHONPATH="$ORCH" "$ORCH/.venv/bin/python" -m orchestrator.cli \
        --instance "$PROJECT" render-agent-docs --team "$TEAM" --function "$DOC_FN" \
        --agent-id "$AGENT_ID" --out-dir "$WORKTREE" >/dev/null 2>&1 \
        && echo "== agent docs re-rendered from SoT ==" \
        || echo "== WARNING: agent-doc SoT render failed (launching anyway; live adr_for_issue still current) ==" >&2
    fi
    ;;
esac

# Model tiering: when launching the CLAUDE runtime for a dev role, append the
# lane's tiering policy (BE_DEV_CLAUDE.md / FE_DEV_CLAUDE.md) onto the freshly
# rendered worktree CLAUDE.md, so the Claude session reads it as project
# instructions (backend -> wt-backend-dev, frontend -> wt-frontend-dev). Appended
# AFTER the SoT render so the render doesn't wipe it; the grep guard avoids a
# double-append if the render was skipped (CLAUDE.md already carries the block).
if [ "$RUNTIME" = "claude" ] && [ "${DOC_FN:-}" = "dev" ] \
   && [ -n "${WORKTREE:-}" ] && [ -f "$WORKTREE/CLAUDE.md" ]; then
  TIER_FILE=""
  case "$TEAM" in
    backend)  TIER_FILE="$WORKSPACE_ROOT/BE_DEV_CLAUDE.md" ;;
    frontend) TIER_FILE="$WORKSPACE_ROOT/FE_DEV_CLAUDE.md" ;;
  esac
  if [ -n "$TIER_FILE" ] && [ -f "$TIER_FILE" ] \
     && ! grep -q "Model Tiering" "$WORKTREE/CLAUDE.md"; then
    { printf '\n\n---\n\n'; cat "$TIER_FILE"; } >> "$WORKTREE/CLAUDE.md"
    echo "== injected $(basename "$TIER_FILE") model-tiering into $WORKTREE/CLAUDE.md =="
  fi
fi

enable_agent_loop
exec "$ADAPTER" "$@"
