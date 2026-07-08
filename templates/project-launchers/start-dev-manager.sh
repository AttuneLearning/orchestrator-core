#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
TEAM="${1:?usage: start-dev-manager.sh <backend|frontend> [runtime] [args...]}"
shift
LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done
case "$TEAM" in
  backend|be) ROLE=backend-dev-manager ;;
  frontend|fe) ROLE=frontend-dev-manager ;;
  *) echo "usage: start-dev-manager.sh <backend|frontend> [runtime] [args...]" >&2; exit 1 ;;
esac
RUNTIME=claude
if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
  RUNTIME="$1"
  shift
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" "$RUNTIME" "$@"
