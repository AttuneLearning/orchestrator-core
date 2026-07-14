#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
TEAM="${1:?usage: start-qa-worker.sh <backend|frontend> [runtime] [args...]}"
shift
case "$TEAM" in
  backend|be) ROLE=backend-qa-worker ;;
  frontend|fe) ROLE=frontend-qa-worker ;;
  *) echo "usage: start-qa-worker.sh <backend|frontend> [runtime] [args...]" >&2; exit 1 ;;
esac
# Parse launch flags anywhere on the line (not just before the runtime); the
# runtime is the first bare runtime name, everything else is a runtime arg.
LAUNCH_FLAGS=()
PASSTHRU=()
RUNTIME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive) LAUNCH_FLAGS+=("$1"); shift ;;
    -m|--model) LAUNCH_FLAGS+=("$1" "${2:?-m/--model requires a value}"); shift 2 ;;
    --model=*) LAUNCH_FLAGS+=("$1"); shift ;;
    -h|--help) exec "$WS/start-agent.sh" --help ;;
    claude|codex|opencode|qwen|qwen-code)
      if [ -z "$RUNTIME" ]; then RUNTIME="$1"; else PASSTHRU+=("$1"); fi
      shift ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" "${RUNTIME:-opencode}" "${PASSTHRU[@]}"
