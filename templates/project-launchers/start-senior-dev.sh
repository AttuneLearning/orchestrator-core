#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
source "$WS/agent-launchers/lib.sh"

usage() {
  cat <<EOF
usage: ./start-senior-dev.sh [runtime] [flags] [runtime args...]

runtime: claude | codex | opencode | qwen | qwen-code   (default: claude)
flags (anywhere on the line):
  --dry-run  --no-enable-loop  --interactive  --non-interactive
  -m/--model MODEL  -h/--help
EOF
}

parse_launch_args "$@"
if [ "$WANT_HELP" = 1 ]; then
  usage
  exit 0
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" senior-dev "${RUNTIME:-claude}" "${PASSTHRU[@]}"
