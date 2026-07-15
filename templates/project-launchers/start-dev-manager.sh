#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
source "$WS/agent-launchers/lib.sh"

usage() {
  cat <<EOF
usage: ./start-dev-manager.sh <backend|frontend> [runtime] [flags] [runtime args...]

runtime: claude | codex | opencode | qwen | qwen-code   (default: claude)
flags (anywhere on the line):
  --dry-run  --no-enable-loop  --interactive  --non-interactive
  -m/--model MODEL  -h/--help
EOF
}

TEAM_ARG="${1:-}"
case "$TEAM_ARG" in
  "") usage >&2; exit 1 ;;
  -h|--help) usage; exit 0 ;;
esac
shift
TEAM="$(resolve_team "$TEAM_ARG")" || { usage >&2; exit 1; }

parse_launch_args "$@"
if [ "$WANT_HELP" = 1 ]; then
  usage
  exit 0
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$TEAM-dev-manager" "${RUNTIME:-claude}" "${PASSTHRU[@]}"
