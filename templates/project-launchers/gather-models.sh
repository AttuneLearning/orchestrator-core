#!/usr/bin/env bash
set -euo pipefail

# Refresh agent-model.yaml from live provider /v1/models endpoints, auto-shorten
# the ids, and update the -m/--model shortcut table.
#
#   ./gather-models.sh              # all providers you have a key for
#   ./gather-models.sh orch_model   # one provider
#   ./gather-models.sh opencode     # all providers for a harness
#   ./gather-models.sh --dry-run    # preview only

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_DIR="$WS/agent-launchers"

[ -f "$LAUNCHER_DIR/orchestrator.env" ] && source "$LAUNCHER_DIR/orchestrator.env"
source "$LAUNCHER_DIR/lib.sh"
ensure_opensource_keys   # MODEL_ACCESS_KEY (DO), QWEN_LOCAL_API_KEY (local)

export WORKSPACE_ROOT="$WS"
PYBIN="${ORCH:+$ORCH/.venv/bin/python}"
PYBIN="${PYBIN:-python3}"
exec "$PYBIN" "$LAUNCHER_DIR/gather-models.py" "$@"
