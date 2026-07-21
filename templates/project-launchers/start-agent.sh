#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$WORKSPACE_ROOT/agent-launchers"

source "$LAUNCHER_DIR/orchestrator.env"
source "$LAUNCHER_DIR/roles.sh"
source "$LAUNCHER_DIR/lib.sh"

usage() {
  cat <<EOF
usage: ./start-agent.sh <role> [runtime] [flags] [runtime args...]

roles:
  orch-manager
  backend-dev-manager | frontend-dev-manager
  backend-dev-worker  | frontend-dev-worker
  backend-qa-worker   | frontend-qa-worker
  senior-dev          | senior-qa

runtime: claude | codex | opencode | qwen | qwen-code
  (default is per-role: managers/senior -> claude, workers -> opencode)

flags (accepted anywhere on the line):
  --dry-run            print the launch plan without starting anything
  --no-enable-loop     do not enable the dashboard agent loop
  --interactive        force the runtime's interactive mode
  --non-interactive    force one-shot/non-interactive mode
  -m, --model MODEL    shortcut or model string valid for the chosen runtime
                       (omit for the runtime default: claude->opus,
                        codex->gpt-5.4-mini, opencode->glm-5.2;
                        list combos: agent-launchers/resolve-model.py --list)
  -h, --help           show this help
EOF
}

# Shared parser: flags land in semantic vars (DRY_RUN, MODEL_SEL, AGENT_MODE,
# AGENT_ENABLE_LOOP), the first bare runtime name lands in RUNTIME, and every
# other bare token lands in PASSTHRU — the first of which is the role.
parse_launch_args "$@"
if [ "$WANT_HELP" = 1 ]; then
  usage
  exit 0
fi
if [ ${#PASSTHRU[@]} -eq 0 ]; then
  usage >&2
  exit 1
fi
ROLE_ARG="${PASSTHRU[0]}"
PASSTHRU=("${PASSTHRU[@]:1}")

resolve_role "$ROLE_ARG"

# Runtime args are only meaningful after an explicit runtime; a bare token with
# no runtime named is almost always a typo'd runtime.
if [ -z "$RUNTIME" ] && [ ${#PASSTHRU[@]} -gt 0 ]; then
  echo "unknown runtime: ${PASSTHRU[0]} (expected one of: $KNOWN_RUNTIMES)" >&2
  exit 1
fi
RUNTIME="${RUNTIME:-$DEFAULT_RUNTIME}"

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
export ROLE ORCH_ROLE="$ROLE" RUNTIME AGENT_ID TEAM FUNCTION GATE APP WORKTREE PROMPT_NAME PROMPT_FILE
export LOOP_AGENT IDLE_STOP COMMAND_TIMEOUT FANOUT_DEFAULT AGENT_POLL_DEFAULT AGENT_ENABLE_LOOP_DEFAULT
export AGENT_MODE

if [ "$DRY_RUN" = "1" ]; then
  print_launch_summary
  echo "model_selection=${MODEL_SEL:-'(default)'}"
  echo "adapter=$ADAPTER"
  echo "extra_args=${PASSTHRU[*]}"
  if [ "$RUNTIME" = "codex" ] || [ "$RUNTIME" = "claude" ] || [ "$RUNTIME" = "opencode" ]; then
    ORCH_LAUNCH_DRY_RUN=1 "$ADAPTER" "${PASSTHRU[@]}"
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
exec "$ADAPTER" "${PASSTHRU[@]}"
