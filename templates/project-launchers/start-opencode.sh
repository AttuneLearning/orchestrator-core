#!/usr/bin/env bash
set -euo pipefail

# Standalone opencode TUI for open-source models, with the orchestrator MCP
# wired in automatically when this workspace was installed through the
# orchestrator config (agent-launchers/orchestrator.env present).
#
# Usage: ./start-opencode.sh [-m MODEL] [--dir PATH] [--dry-run] [-- opencode args]
#   -m/--model  provider/model or alias. Default: deepseek-4-flash
#               aliases: glm-5.2 | glm-5.1 | deepseek(-v4-pro) | qwen(-local/qwen3-coder)
#               open-source menu: orch_model/{glm-5.1,glm-5.2,deepseek-v4-pro}, qwen_local/qwen-local

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_DIR="$WS/agent-launchers"

# orchestrator identity (PROJECT/ORCH/DASHBOARD) -> enables MCP wiring
[ -f "$LAUNCHER_DIR/orchestrator.env" ] && source "$LAUNCHER_DIR/orchestrator.env"
source "$LAUNCHER_DIR/lib.sh"

command -v opencode >/dev/null 2>&1 || { echo "missing opencode CLI on PATH" >&2; exit 1; }

MODEL="orch_model/deepseek-4-flash"
WORKDIR=""
DRY_RUN=0
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--model) MODEL="${2:?-m needs a model}"; shift 2 ;;
    --model=*) MODEL="${1#--model=}"; shift ;;
    --dir) WORKDIR="${2:?--dir needs a path}"; shift 2 ;;
    --dir=*) WORKDIR="${1#--dir=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; PASS+=("$@"); break ;;
    *) PASS+=("$1"); shift ;;
  esac
done

# Resolve the shortcut against agent-model.yaml (opencode harness) when this is
# an orchestrator-installed workspace; fall back to inline aliases otherwise.
if [ -n "${ORCH:-}" ] && [ -f "$LAUNCHER_DIR/resolve-model.py" ]; then
  MODEL="$("$ORCH/.venv/bin/python" "$LAUNCHER_DIR/resolve-model.py" opencode "$MODEL" 2>/dev/null || echo "$MODEL")"
fi
case "$MODEL" in
  glm5.2|glm-5.2) MODEL="orch_model/glm-5.2" ;;
  glm5.1|glm-5.1) MODEL="orch_model/glm-5.1" ;;
  deepseek|dsv4pro|deepseek-v4-pro) MODEL="orch_model/deepseek-v4-pro" ;;
  qwen|qwen-local|qwen3-coder) MODEL="qwen_local/qwen-local" ;;
esac

ensure_opensource_keys

if [ -z "$WORKDIR" ]; then
  for cand in "$WS/humantest-wt" "$WS/wt-human-test" "$WS"; do
    [ -d "$cand" ] && { WORKDIR="$cand"; break; }
  done
fi

tmpcfg="$(mktemp -d "${TMPDIR:-/tmp}/opencode-cli.XXXXXX")"
cleanup() { rm -rf "$tmpcfg"; }
trap cleanup INT TERM
mkdir -p "$tmpcfg/opencode"
export XDG_CONFIG_HOME="$tmpcfg"
CFG="$tmpcfg/opencode/opencode.jsonc"
opencode_write_config "$CFG" "$MODEL"

if [ "$DRY_RUN" = "1" ]; then
  echo "runtime=opencode"
  echo "model=$MODEL"
  echo "project=${PROJECT:-'(none)'}"
  echo "mcp=$([ -n "${ORCH:-}" ] && [ -n "${PROJECT:-}" ] && echo "orchestrator (--instance ${PROJECT} serve)" || echo '(none - not an orchestrator-installed workspace)')"
  echo "api_key_configured=$([ -n "${MODEL_ACCESS_KEY:-}" ] && echo yes || echo no)"
  echo "worktree=$WORKDIR"
  echo "config=$CFG"
  echo "--- config ---"
  cat "$CFG"
  cleanup
  exit 0
fi

cd "$WORKDIR"
exec opencode --model "$MODEL" "${PASS[@]}"
