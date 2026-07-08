#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop) LAUNCH_FLAGS+=("$1"); shift ;;
    *) break ;;
  esac
done
RUNTIME=claude
if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
  RUNTIME="$1"
  shift
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" orch-manager "$RUNTIME" "$@"
