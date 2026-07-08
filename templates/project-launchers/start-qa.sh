#!/usr/bin/env bash
set -euo pipefail

WS="$(cd "$(dirname "$0")" && pwd)"
WTN="${1:?usage: start-qa.sh <wt-backend-qa|wt-frontend-qa> [args...]}"
shift
LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done

case "$WTN" in
  wt-backend-qa) ROLE=backend-qa-worker ;;
  wt-frontend-qa) ROLE=frontend-qa-worker ;;
  *) echo "usage: start-qa.sh <wt-backend-qa|wt-frontend-qa> [args...]" >&2; exit 1 ;;
esac

exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" codex "$@"
