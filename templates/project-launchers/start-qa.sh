#!/usr/bin/env bash
set -euo pipefail

WS="$(cd "$(dirname "$0")" && pwd)"
WTN="${1:?usage: start-qa.sh <wt-backend-qa|wt-frontend-qa> [args...]}"
shift
case "$WTN" in
  wt-backend-qa) ROLE=backend-qa-worker ;;
  wt-frontend-qa) ROLE=frontend-qa-worker ;;
  *) echo "usage: start-qa.sh <wt-backend-qa|wt-frontend-qa> [args...]" >&2; exit 1 ;;
esac

# Parse launch flags anywhere on the line; the runtime is fixed to codex here,
# everything else is a runtime arg.
LAUNCH_FLAGS=()
PASSTHRU=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--no-enable-loop|--interactive|--non-interactive) LAUNCH_FLAGS+=("$1"); shift ;;
    -m|--model) LAUNCH_FLAGS+=("$1" "${2:?-m/--model requires a value}"); shift 2 ;;
    --model=*) LAUNCH_FLAGS+=("$1"); shift ;;
    -h|--help) exec "$WS/start-agent.sh" --help ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done

exec "$WS/start-agent.sh" "${LAUNCH_FLAGS[@]}" "$ROLE" codex "${PASSTHRU[@]}"
