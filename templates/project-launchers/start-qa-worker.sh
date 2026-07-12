#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
TEAM="${1:?usage: start-qa-worker.sh <backend|frontend> [runtime] [args...]}"
shift
LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive) LAUNCH_FLAGS+=("$1"); shift ;;
    -m|--model) LAUNCH_FLAGS+=("$1" "${2:?-m/--model requires a value}"); shift 2 ;;
    --model=*) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done
case "$TEAM" in
  backend|be) ROLE=backend-qa-worker ;;
  frontend|fe) ROLE=frontend-qa-worker ;;
  *) echo "usage: start-qa-worker.sh <backend|frontend> [runtime] [args...]" >&2; exit 1 ;;
esac
RUNTIME=opencode
if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
  RUNTIME="$1"
  shift
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" "$RUNTIME" "$@"
