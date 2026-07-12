#!/usr/bin/env bash
set -euo pipefail

WS="$(cd "$(dirname "$0")" && pwd)"
WTN="${1:?usage: start-claude-dev.sh <wt-backend-dev|wt-frontend-dev> [args...]}"
shift
LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done

case "$WTN" in
  wt-backend-dev) ROLE=backend-dev-manager ;;
  wt-frontend-dev) ROLE=frontend-dev-manager ;;
  *) echo "usage: start-claude-dev.sh <wt-backend-dev|wt-frontend-dev> [args...]" >&2; exit 1 ;;
esac

exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" claude "$@"
