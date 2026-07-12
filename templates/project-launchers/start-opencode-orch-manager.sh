#!/usr/bin/env bash
set -euo pipefail

# Launch OpenCode as the orch-manager (orchestrator MCP + orch-manager prompt,
# from the workspace root). Model selectable with -m against agent-model.yaml
# (opencode harness): glm-5.2 (default) | glm-5.1 | deepseek | qwen-local | ...
#   list: agent-launchers/resolve-model.py opencode --list

WS="$(cd "$(dirname "$0")" && pwd)"

LAUNCH_FLAGS=(--no-enable-loop)
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive)
      LAUNCH_FLAGS+=("$1"); shift ;;
    -m|--model) LAUNCH_FLAGS+=("$1" "${2:?-m/--model requires a value}"); shift 2 ;;
    --model=*) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done

exec "$WS/start-orch-manager.sh" "${LAUNCH_FLAGS[@]}" opencode "$@"
