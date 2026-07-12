#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"

TEAM="${1:-}"
if [ -z "$TEAM" ]; then
  echo "usage: start-qwen-code-worker.sh <backend|frontend> [args...]" >&2
  exit 1
fi
shift

LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done

exec "$WS/start-dev-worker.sh" "$TEAM" "${LAUNCH_FLAGS[@]}" qwen-code "$@"
