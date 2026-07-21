#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

PROMPT="$(render_prompt "$PROMPT_FILE")"
DEFAULT_AGENT_MODE="non-interactive"
if [ "${ROLE:-}" = "orch-manager" ]; then
  DEFAULT_AGENT_MODE="interactive"
fi
LAUNCH_MODE="$(resolve_agent_mode "$DEFAULT_AGENT_MODE")"

INFERENCE_PROFILE="${CODEX_INFERENCE_PROFILE:-}"
INFERENCE_EXPLICIT=0
CODEX_PROVIDER_CONFIG=()
RUNTIME_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --inference)
      if [ $# -lt 2 ]; then
        echo "--inference requires a profile name: digitalocean or openai-custom" >&2
        exit 2
      fi
      INFERENCE_PROFILE="$2"
      INFERENCE_EXPLICIT=1
      shift 2
      ;;
    --inference=*)
      INFERENCE_PROFILE="${1#--inference=}"
      INFERENCE_EXPLICIT=1
      shift
      ;;
    --digital-ocean-config)
      INFERENCE_PROFILE="digitalocean"
      INFERENCE_EXPLICIT=1
      shift
      ;;
    --openai-custom-config)
      INFERENCE_PROFILE="openai-custom"
      INFERENCE_EXPLICIT=1
      shift
      ;;
    *)
      RUNTIME_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ "$INFERENCE_EXPLICIT" = "0" ] && [ -x "$LAUNCHER_DIR/model-settings.py" ]; then
  eval "$("$LAUNCHER_DIR/model-settings.py" orch_manager_codex)"
  INFERENCE_PROFILE="${ORCH_MODEL_PROFILE:-}"
fi

case "$INFERENCE_PROFILE" in
  "")
    ;;
  digitalocean|do)
    if [ "$INFERENCE_EXPLICIT" = "1" ]; then
      model="deepseek-v4-pro"
      base_url="https://inference.do-ai.run/v1"
      wire_api="chat"
      reasoning_effort="high"
    else
      model="${ORCH_MODEL_NAME:-deepseek-v4-pro}"
      base_url="${ORCH_MODEL_BASE_URL:-https://inference.do-ai.run/v1}"
      wire_api="${ORCH_MODEL_WIRE_API:-chat}"
      reasoning_effort="${ORCH_MODEL_REASONING_EFFORT:-high}"
    fi
    if [ "$INFERENCE_EXPLICIT" = "0" ] && [ -n "${ORCH_MODEL_API_KEY:-}" ]; then
      export MODEL_ACCESS_KEY="$ORCH_MODEL_API_KEY"
    fi
    CODEX_PROVIDER_CONFIG+=(
      -c "model=\"$model\""
      -c 'features.image_generation=false'
      -c 'preferred_auth_method="apikey"'
      -c "model_reasoning_effort=\"$reasoning_effort\""
      -c 'model_provider="digitalocean"'
      -c 'model_providers.digitalocean.name="DigitalOcean AI"'
      -c "model_providers.digitalocean.base_url=\"$base_url\""
      -c 'model_providers.digitalocean.env_key="MODEL_ACCESS_KEY"'
      -c "model_providers.digitalocean.wire_api=\"$wire_api\""
      -c 'model_providers.digitalocean.query_params={}'
    )
    ;;
  openai-custom|openai_custom|proxy)
    base_url=""
    if [ "$INFERENCE_EXPLICIT" = "0" ]; then
      base_url="${ORCH_MODEL_BASE_URL:-}"
    fi
    if [ -z "$base_url" ] && [ -n "${INFERENCE_PROXY_BASE_URL:-}" ]; then
      base_url="${INFERENCE_PROXY_BASE_URL%/}/v1"
    fi
    if [ -z "$base_url" ]; then
      echo "INFERENCE_PROXY_BASE_URL is required for --inference openai-custom" >&2
      exit 2
    fi
    if [ "$INFERENCE_EXPLICIT" = "1" ]; then
      model="deepseek-4-flash"
      wire_api="responses"
      reasoning_effort="high"
    else
      model="${ORCH_MODEL_NAME:-deepseek-4-flash}"
      wire_api="${ORCH_MODEL_WIRE_API:-responses}"
      reasoning_effort="${ORCH_MODEL_REASONING_EFFORT:-high}"
    fi
    if [ "$INFERENCE_EXPLICIT" = "0" ] && [ -n "${ORCH_MODEL_API_KEY:-}" ]; then
      export MODEL_ACCESS_KEY="$ORCH_MODEL_API_KEY"
    fi
    CODEX_PROVIDER_CONFIG+=(
      -c "model=\"$model\""
      -c 'features.image_generation=false'
      -c 'preferred_auth_method="apikey"'
      -c "model_reasoning_effort=\"$reasoning_effort\""
      -c 'model_provider="openai_custom"'
      -c 'model_providers.openai_custom.name="OpenAI Compatible"'
      -c "model_providers.openai_custom.base_url=\"$base_url\""
      -c 'model_providers.openai_custom.env_key="MODEL_ACCESS_KEY"'
      -c "model_providers.openai_custom.wire_api=\"$wire_api\""
      -c 'model_providers.openai_custom.query_params={}'
    )
    ;;
  qwen-local|qwen_local)
    provider="${ORCH_MODEL_PROVIDER:-qwen_local}"
    model="${ORCH_MODEL_NAME:-}"
    base_url="${ORCH_MODEL_BASE_URL:-}"
    wire_api="${ORCH_MODEL_WIRE_API:-chat}"
    reasoning_effort="${ORCH_MODEL_REASONING_EFFORT:-high}"
    if [ -z "$model" ] || [ -z "$base_url" ]; then
      echo "qwen-local profile requires model_profiles.qwen-local.model and base_url" >&2
      exit 2
    fi
    if [ -n "${ORCH_MODEL_API_KEY:-}" ]; then
      export MODEL_ACCESS_KEY="$ORCH_MODEL_API_KEY"
    fi
    CODEX_PROVIDER_CONFIG+=(
      -c "model=\"$model\""
      -c 'features.image_generation=false'
      -c 'preferred_auth_method="apikey"'
      -c "model_reasoning_effort=\"$reasoning_effort\""
      -c "model_provider=\"$provider\""
      -c "model_providers.${provider}.name=\"OpenAI Compatible\""
      -c "model_providers.${provider}.base_url=\"$base_url\""
      -c "model_providers.${provider}.env_key=\"MODEL_ACCESS_KEY\""
      -c "model_providers.${provider}.wire_api=\"$wire_api\""
      -c "model_providers.${provider}.query_params={}"
    )
    ;;
  *)
    echo "unknown Codex inference profile: $INFERENCE_PROFILE (expected digitalocean, openai-custom, or qwen-local)" >&2
    exit 2
    ;;
esac

# Explicit -m/--model on the OpenAI (native) path: no provider override is set,
# so just pin the model. (Profile paths already set their own model above.)
if [ -z "$INFERENCE_PROFILE" ] && [ -n "${ORCH_CODEX_MODEL:-}" ]; then
  CODEX_PROVIDER_CONFIG+=( -c "model=\"$ORCH_CODEX_MODEL\"" )
fi

MCP_CONFIG=(
  -c "mcp_servers.orchestrator.args=[\"-m\",\"orchestrator.cli\",\"--instance\",\"$PROJECT\",\"serve\"]"
  -c "mcp_servers.orchestrator.command=\"$ORCH/.venv/bin/python\""
  -c "mcp_servers.orchestrator.env.PYTHONPATH=\"$ORCH\""
  -c "mcp_servers.orchestrator.env.ORCH_ROLE=\"$ROLE\""
)

if [ "${ROLE:-}" = "orch-manager" ]; then
  if [ "$LAUNCH_MODE" = "interactive" ]; then
    cmd=(codex --yolo -C "$WORKTREE" "${MCP_CONFIG[@]}" "${CODEX_PROVIDER_CONFIG[@]}" "${RUNTIME_ARGS[@]}" "$PROMPT")
  else
    cmd=(codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
      -C "$WORKTREE" "${MCP_CONFIG[@]}" "${CODEX_PROVIDER_CONFIG[@]}" "${RUNTIME_ARGS[@]}" "$PROMPT")
  fi
else
  if [ "$LAUNCH_MODE" = "interactive" ]; then
    cmd=(codex --yolo -C "$WORKTREE" "${MCP_CONFIG[@]}" "${CODEX_PROVIDER_CONFIG[@]}" "${RUNTIME_ARGS[@]}" "$PROMPT")
  else
    cmd=(codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
      -C "$WORKTREE" "${MCP_CONFIG[@]}" "${CODEX_PROVIDER_CONFIG[@]}" "${RUNTIME_ARGS[@]}" "$PROMPT")
  fi
fi

if [ "${ORCH_LAUNCH_DRY_RUN:-0}" = "1" ]; then
  if [ "$INFERENCE_EXPLICIT" = "0" ] && [ -x "$LAUNCHER_DIR/model-settings.py" ]; then
    "$LAUNCHER_DIR/model-settings.py" orch_manager_codex --diagnostic
  else
    echo "profile=${INFERENCE_PROFILE:-'(none)'}"
    echo "api_key_configured=$([ -n "${MODEL_ACCESS_KEY:-}" ] && echo yes || echo no)"
  fi
  printf 'command='
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

# Loop by default, INCLUDING interactive: run-agent-loop keeps the agent cycling
# (re-launches the command each cycle) instead of stopping after one session.
# LOOP_AGENT=1 keeps non-interactive roles looping too.
if [ -n "${AGENT_ID:-}" ] && { [ "$LAUNCH_MODE" = "interactive" ] || [ "${LOOP_AGENT:-0}" = "1" ]; }; then
  export ORCH_DASHBOARD="$DASHBOARD"
  export AGENT_POLL="${AGENT_POLL:-${AGENT_POLL_DEFAULT:-90}}"
  if [ "${IDLE_STOP:-0}" -gt 0 ]; then
    export AGENT_IDLE_STOP="$IDLE_STOP"
  fi
  exec "$WORKSPACE_ROOT/run-agent-loop.sh" "$AGENT_ID" "${cmd[@]}"
fi

exec "${cmd[@]}"
