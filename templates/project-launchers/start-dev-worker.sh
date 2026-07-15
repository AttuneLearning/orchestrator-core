#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "$0")" && pwd)"
source "$WS/agent-launchers/lib.sh"

usage() {
  cat <<EOF
usage: ./start-dev-worker.sh <backend|frontend|backend-2> [runtime] [flags] [runtime args...]
  backend-2: second backend dev lane (agent 8, wt-backend-dev-2) — needs the
             backend-dev-worker-2 role in roles.sh + a registered agent + worktree.

runtime: claude | codex | opencode | qwen | qwen-code   (default: opencode)
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
# Extra parallel lanes route to their own role (own agent_id + worktree in roles.sh);
# plain backend/frontend resolve to the primary dev-worker role.
case "$TEAM_ARG" in
  backend-2|backend2|be-dev-2|be-2) ROLE="backend-dev-worker-2" ;;
  *) TEAM="$(resolve_team "$TEAM_ARG")" || { usage >&2; exit 1; }
     ROLE="$TEAM-dev-worker" ;;
esac

parse_launch_args "$@"
if [ "$WANT_HELP" = 1 ]; then
  usage
  exit 0
fi
exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" "${RUNTIME:-opencode}" "${PASSTHRU[@]}"
