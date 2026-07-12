#!/usr/bin/env bash
set -euo pipefail

WS="$(cd "$(dirname "$0")" && pwd)"

LAUNCH_FLAGS=(--no-enable-loop)
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive)
      LAUNCH_FLAGS+=("$1")
      shift
      ;;
    *)
      break
      ;;
  esac
done

echo "Starting Qwen Code CLI orch-manager..."

exec "$WS/start-orch-manager.sh" "${LAUNCH_FLAGS[@]}" qwen-code "$@"
