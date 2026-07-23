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

# ---------------------------------------------------------------------------
# AGENT_SIDECAR=1 (durable-worker-sidecar plan, Phase 5): route through the
# side-car (agent-launchers/sidecar.py) instead of the per-cycle relaunch
# loop (run-agent-loop.sh). The worker session becomes durable -- the
# side-car injects "tick" prompts into the SAME session/pane forever instead
# of re-execing the agent every poll. Opt-in per launch; AGENT_SIDECAR
# unset/0 (the default) skips this whole block and behavior stays
# byte-identical to today.
# ---------------------------------------------------------------------------
if [ "${AGENT_SIDECAR:-0}" = "1" ]; then
  if [ -z "${AGENT_ID:-}" ]; then
    echo "AGENT_SIDECAR=1 requires a role with an AGENT_ID ('$ROLE' has none)" >&2
    exit 1
  fi

  # Render the worker prompt EXACTLY as an interactive/TUI launch would today
  # (render_prompt + apply_interactive_prompt interactive mode strips the
  # "one cycle then STOP" directive so it can't fight the tick contract) --
  # written to its own temp file since sidecar.py takes --prompt-file, not
  # the rendered text directly. Left in place for the sidecar's whole
  # lifetime (it re-reads --prompt-file only at its own startup, but the
  # path must stay valid for that one read).
  SIDECAR_PROMPT_FILE="$(mktemp "${TMPDIR:-/tmp}/sidecar-prompt-${PROJECT:-workspace}-${AGENT_ID}.XXXXXX")"
  RENDERED_PROMPT="$(render_prompt "$PROMPT_FILE")"
  RENDERED_PROMPT="$(apply_interactive_prompt "$RENDERED_PROMPT" interactive)"
  # DEFECT-SIDECAR-1 fix (plan §14): the side-car's parse_tick_result requires
  # the worker to END every reply with a `TICK RESULT: ...` line, but the role
  # prompt alone never tells it to -- so without this the FIRST soak saw every
  # tick parse invalid=False (2026-07-23). Append the tick contract to the
  # injected prompt so every tick carries the marker grammar the parser needs.
  SIDECAR_TICK_CONTRACT="$LAUNCHER_DIR/prompts/tick-contract.md"
  if [ ! -f "$SIDECAR_TICK_CONTRACT" ]; then
    echo "AGENT_SIDECAR=1: missing tick contract at $SIDECAR_TICK_CONTRACT" >&2
    exit 1
  fi
  RENDERED_TICK_CONTRACT="$(render_prompt "$SIDECAR_TICK_CONTRACT")"
  printf '%s\n\n%s\n' "$RENDERED_PROMPT" "$RENDERED_TICK_CONTRACT" > "$SIDECAR_PROMPT_FILE"

  SIDECAR_ARGS=(--agent-id "$AGENT_ID" --project "$PROJECT" --dashboard "$DASHBOARD"
                --prompt-file "$SIDECAR_PROMPT_FILE")

  case "$RUNTIME" in
    opencode)
      # Per-agent port so multiple sidecar'd opencode workers never collide --
      # QA fix: 4900+AGENT_ID alone collides ACROSS PROJECTS (both fleets
      # share this host, and agent-id numbering is small/reused per project,
      # e.g. agent 1 in cadencelms and agent 1 in tendcharting would both
      # claim 4901). Fold a hash of PROJECT into the base so different
      # projects land in different 20-wide port bands; cksum's mod 50 keeps
      # the whole range within ~4900-5920 (50 bands x 20 ports/band).
      SIDECAR_PORT=$((4900 + ($(printf '%s' "$PROJECT" | cksum | cut -d' ' -f1) % 50) * 20 + AGENT_ID))
      # Same model resolution opencode.sh uses: ORCH_OPENCODE_MODEL (set by
      # -m/--model above) or the glm-5.2 default; split provider/model on
      # the slash the same way opencode.sh's config does.
      SIDECAR_OPENCODE_MODEL="${ORCH_OPENCODE_MODEL:-orch_model/glm-5.2}"
      SIDECAR_PROVIDER_ID="${SIDECAR_OPENCODE_MODEL%%/*}"
      SIDECAR_MODEL_ID="${SIDECAR_OPENCODE_MODEL#*/}"

      command -v opencode >/dev/null 2>&1 || { echo "missing opencode CLI on PATH" >&2; exit 1; }
      ensure_opensource_keys
      # opencode_write_config needs its own XDG_CONFIG_HOME (provider menu +
      # orchestrator MCP), same as opencode.sh's normal launch -- but unlike
      # that per-launch temp dir (cleaned up on exit), this one must persist
      # for the whole life of the durable `opencode serve` process sidecar.py
      # spawns (and any restart of it), so it is never rm -rf'd here.
      SIDECAR_OC_CONFIG_HOME="$(mktemp -d "${TMPDIR:-/tmp}/opencode-sidecar-${PROJECT:-workspace}-${AGENT_ID}.XXXXXX")"
      mkdir -p "$SIDECAR_OC_CONFIG_HOME/opencode"
      export XDG_CONFIG_HOME="$SIDECAR_OC_CONFIG_HOME"
      opencode_write_config "$SIDECAR_OC_CONFIG_HOME/opencode/opencode.jsonc" "$SIDECAR_OPENCODE_MODEL"

      SIDECAR_ARGS+=(--runtime opencode
                     --opencode-url "http://127.0.0.1:$SIDECAR_PORT"
                     --opencode-dir "$WORKTREE"
                     --opencode-provider-id "$SIDECAR_PROVIDER_ID"
                     --opencode-model-id "$SIDECAR_MODEL_ID")
      ;;
    claude|codex)
      # tmux runtimes: the operator owns a tmux pane running (or ready to run)
      # the TUI; the side-car injects ticks into it via capture-pane/
      # send-keys rather than owning a subprocess itself.
      if [ -z "${SIDECAR_TMUX_TARGET:-}" ]; then
        echo "AGENT_SIDECAR=1 with runtime '$RUNTIME' requires SIDECAR_TMUX_TARGET" \
             "(the operator's tmux pane, e.g. 'agents:1.0')" >&2
        exit 1
      fi
      # spawn_cmd = exactly what this runtime adapter would exec today for an
      # interactive launch -- obtained by asking the adapter itself (not
      # reconstructed here) via its --print-cmd mode, so it can never drift
      # from the real interactive exec line (MCP wiring, model flags, etc).
      # QA fix: force COMMAND_TIMEOUT=0 for this call -- a durable side-car
      # session must NEVER be wrapped in `timeout N` (the side-car's own
      # watchdog, not a per-cycle shell timeout, owns stuck detection; some
      # roles default COMMAND_TIMEOUT>0, e.g. dev-manager's 1200s, which
      # exists only for the per-cycle relaunch loop this bypasses).
      if ! SIDECAR_SPAWN_CMD="$(AGENT_MODE=interactive COMMAND_TIMEOUT=0 "$ADAPTER" --print-cmd "${PASSTHRU[@]}")"; then
        echo "failed to construct the tmux spawn command via '$ADAPTER --print-cmd'" >&2
        exit 1
      fi
      SIDECAR_ARGS+=(--runtime tmux --tmux-target "$SIDECAR_TMUX_TARGET"
                     --tmux-spawn-cmd "$SIDECAR_SPAWN_CMD")
      ;;
    *)
      echo "AGENT_SIDECAR=1 is not supported for runtime '$RUNTIME' (expected claude, codex, or opencode)" >&2
      exit 1
      ;;
  esac

  # Foreground; Ctrl-C exits the side-car cleanly (phase-2), leaving the
  # durable worker session/pane alive by default.
  exec python3 "$LAUNCHER_DIR/sidecar.py" "${SIDECAR_ARGS[@]}"
fi

exec "$ADAPTER" "${PASSTHRU[@]}"
