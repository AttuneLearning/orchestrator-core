#!/usr/bin/env bash

render_prompt() {
  local prompt_file="$1"
  if [ ! -f "$prompt_file" ]; then
    echo "missing prompt file: $prompt_file" >&2
    return 1
  fi
  ROLE="$ROLE" AGENT_ID="${AGENT_ID:-}" TEAM="${TEAM:-}" FUNCTION="${FUNCTION:-}" \
    GATE="${GATE:-}" APP="${APP:-}" PROJECT="$PROJECT" WORKTREE="$WORKTREE" \
    FANOUT="${FANOUT:-${FANOUT_DEFAULT:-3}}" \
    perl -pe 's/\{\{([A-Z0-9_]+)\}\}/exists $ENV{$1} ? $ENV{$1} : ""/ge' "$prompt_file"
}

resolve_agent_mode() {
  local default_mode="${1:-non-interactive}"
  case "${AGENT_MODE:-}" in
    "")
      printf '%s\n' "$default_mode"
      ;;
    interactive|non-interactive)
      printf '%s\n' "$AGENT_MODE"
      ;;
    *)
      echo "unknown AGENT_MODE: ${AGENT_MODE:-} (expected interactive or non-interactive)" >&2
      return 2
      ;;
  esac
}

enable_agent_loop() {
  if [ -z "${AGENT_ID:-}" ] || [ "${AGENT_ENABLE_LOOP:-${AGENT_ENABLE_LOOP_DEFAULT:-1}}" != "1" ]; then
    return 0
  fi
  PYTHONPATH="$ORCH${PYTHONPATH:+:$PYTHONPATH}" ORCH_INSTANCE="$PROJECT" \
    "$ORCH/.venv/bin/python" -m orchestrator.cli --instance "$PROJECT" \
    agent-loop --agent "$AGENT_ID" --enable >/dev/null || {
      echo "warning: could not enable dashboard loop for agent $AGENT_ID" >&2
      return 0
    }
}

print_launch_summary() {
  cat <<EOF
role=$ROLE
runtime=$RUNTIME
workspace=$WORKSPACE_ROOT
worktree=$WORKTREE
project=$PROJECT
orchestrator=$ORCH
dashboard=$DASHBOARD
agent_id=${AGENT_ID:-}
team=${TEAM:-}
function=${FUNCTION:-}
gate=${GATE:-}
app=${APP:-}
prompt=$PROMPT_FILE
loop_agent=$LOOP_AGENT
agent_mode=${AGENT_MODE:-'(default)'}
EOF
}

# ---------------------------------------------------------------------------
# Shared runtime config: the SINGLE definition of the open-source model menu
# and the orchestrator MCP wiring. Every opencode launcher (runtime adapter
# and standalone start-opencode-*.sh) calls opencode_write_config so the model
# menu + MCP are identical everywhere. The MCP block is included only when the
# workspace was installed through the orchestrator config (ORCH + PROJECT set,
# from agent-launchers/orchestrator.env), satisfying "MCP available to any
# agent started from an orchestrator-installed directory".
#
# Open-source endpoints (opencode only):
#   orch_model  -> DigitalOcean OpenAI-compatible: glm-5.1, glm-5.2, deepseek-v4-pro
#   qwen_local  -> local qwen3-coder server:        qwen-local
# Anthropic (claude) and OpenAI (codex) keep their own native config/auth.
# ---------------------------------------------------------------------------

ORCH_DO_BASE_URL_DEFAULT="https://inference.do-ai.run/v1"
QWEN_LOCAL_BASE_URL_DEFAULT="http://10.100.90.132:8083/v1"

# opencode_write_config <output.jsonc> <provider/model>
# Emits the opencode config. Providers + model menu are read from agent-model.yaml
# (so `gather-models.sh` refreshes what opencode offers); falls back to a built-in
# menu if the yaml / PyYAML is unavailable. The orchestrator MCP block is added
# only when ORCH+PROJECT are set (i.e. an orchestrator-installed workspace).
opencode_write_config() {
  local out="$1" model="$2" lib_dir yaml pybin
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  yaml="${AGENT_MODEL_YAML:-}"
  [ -z "$yaml" ] && [ -n "${WORKSPACE_ROOT:-}" ] && [ -f "$WORKSPACE_ROOT/agent-model.yaml" ] && yaml="$WORKSPACE_ROOT/agent-model.yaml"
  [ -z "$yaml" ] && [ -f "$lib_dir/../agent-model.yaml" ] && yaml="$lib_dir/../agent-model.yaml"
  pybin="${ORCH:+$ORCH/.venv/bin/python}"; pybin="${pybin:-python3}"
  OC_OUT="$out" \
  OC_MODEL="$model" \
  OC_ORCH="${ORCH:-}" \
  OC_PROJECT="${PROJECT:-}" \
  OC_YAML="$yaml" \
  OC_DO_URL="${ORCH_DO_BASE_URL:-$ORCH_DO_BASE_URL_DEFAULT}" \
  OC_QWEN_URL="${QWEN_LOCAL_BASE_URL:-$QWEN_LOCAL_BASE_URL_DEFAULT}" \
  "$pybin" - <<'PY'
import json, os

model = os.environ["OC_MODEL"]
orch = os.environ.get("OC_ORCH") or ""
project = os.environ.get("OC_PROJECT") or ""

def _fallback():
    return {
        "orch_model": {"name": "DigitalOcean (OpenAI-Compatible)", "base_url": os.environ["OC_DO_URL"],
                       "key_env": "MODEL_ACCESS_KEY", "models": ["glm-5.1", "glm-5.2", "deepseek-v4-pro"]},
        "qwen_local": {"name": "Local Qwen (qwen3-coder)", "base_url": os.environ["OC_QWEN_URL"],
                       "key_env": "QWEN_LOCAL_API_KEY", "models": ["qwen-local"]},
    }

prov = {}
try:
    import yaml as _y
    with open(os.environ["OC_YAML"], encoding="utf-8") as fh:
        data = _y.safe_load(fh) or {}
    pdefs = {k: v for k, v in (data.get("providers") or {}).items()
             if (v or {}).get("harness") == "opencode"}
    ocmodels = ((data.get("harnesses", {}) or {}).get("opencode", {}) or {}).get("models", {}) or {}
    for pid, pdef in pdefs.items():
        prov[pid] = {"name": pid, "base_url": (pdef or {}).get("base_url", ""),
                     "key_env": (pdef or {}).get("api_key_env", "MODEL_ACCESS_KEY"), "models": []}
    for _short, spec in ocmodels.items():
        ms = str((spec or {}).get("model", ""))
        if "/" in ms:
            pid, mid = ms.split("/", 1)
            if pid in prov and mid not in prov[pid]["models"]:
                prov[pid]["models"].append(mid)
    prov = {k: v for k, v in prov.items() if v["models"]}
    if not prov:
        prov = _fallback()
except Exception:
    prov = _fallback()

cfg = {"$schema": "https://opencode.ai/config.json", "provider": {},
       "model": model, "small_model": model}
for pid, p in prov.items():
    cfg["provider"][pid] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": p["name"],
        "options": {"baseURL": p["base_url"], "apiKey": "{env:%s}" % p["key_env"]},
        "models": {m: {} for m in p["models"]},
    }

if orch and project:
    cfg["mcp"] = {
        "orchestrator": {
            "type": "local",
            "command": [f"{orch}/.venv/bin/python", "-m", "orchestrator.cli",
                        "--instance", project, "serve"],
            "environment": {"PYTHONPATH": orch, "ORCH_INSTANCE": project},
            "enabled": True,
        }
    }

with open(os.environ["OC_OUT"], "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
}

# write_mcp_json <output.json>
# Emits a Claude Code / generic mcpServers JSON pointing at this project's
# orchestrator coordinator. Returns non-zero (writes nothing) if the workspace
# is not orchestrator-installed (ORCH/PROJECT unset).
write_mcp_json() {
  local out="$1"
  [ -n "${ORCH:-}" ] && [ -n "${PROJECT:-}" ] || return 1
  MJ_OUT="$out" MJ_ORCH="$ORCH" MJ_PROJECT="$PROJECT" python3 - <<'PY'
import json, os
orch = os.environ["MJ_ORCH"]; project = os.environ["MJ_PROJECT"]
cfg = {"mcpServers": {"orchestrator": {
    "command": f"{orch}/.venv/bin/python",
    "args": ["-m", "orchestrator.cli", "--instance", project, "serve"],
    "env": {"PYTHONPATH": orch, "ORCH_INSTANCE": project},
}}}
with open(os.environ["MJ_OUT"], "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
}

# ensure_opensource_keys: make sure both open-source provider key envs are set
# so opencode can resolve {env:...} for whichever provider is selected. When a
# key is not already in the environment (e.g. a fresh tmux pane that did not
# source your shell rc), it is loaded from the gitignored secrets file
# agent-launchers/secrets.env — the durable, per-workspace key source.
ensure_opensource_keys() {
  local _d secrets
  _d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  secrets="$_d/secrets.env"
  if { [ -z "${MODEL_ACCESS_KEY:-}" ] || [ -z "${QWEN_LOCAL_API_KEY:-}" ]; } && [ -f "$secrets" ]; then
    # shellcheck disable=SC1090
    set -a; source "$secrets"; set +a
  fi
  if [ -z "${MODEL_ACCESS_KEY:-}" ]; then
    export MODEL_ACCESS_KEY="${DO_API_KEY:-}"
  fi
  export QWEN_LOCAL_API_KEY="${QWEN_LOCAL_API_KEY:-blank}"
}
