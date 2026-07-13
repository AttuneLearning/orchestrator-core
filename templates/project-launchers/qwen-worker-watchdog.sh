#!/usr/bin/env bash
# Cron wrapper for worker-watchdog.py (stamped by setup-project/install-launchers).
# Cron runs with a bare environment, so we restore PATH (node/opencode) and the
# worker env before the watchdog can relaunch a worker. Enable via ./install-watchdog.sh.
set -euo pipefail
WS="__WORKSPACE_ROOT__"
ORCH_PY="__ORCH_PATH__/.venv/bin/python"

# Restore an interactive-like PATH so start-agent.sh finds node/npm/opencode/git.
export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin:$PATH"
export ORCH_INSTANCE="__PROJECT_NAME__"
# Model for relaunched dev workers (override in the crontab line if desired):
export WATCHDOG_MODEL="${WATCHDOG_MODEL:-qwen-local}"

# Load worker secrets/env if present (MODEL_ACCESS_KEY, provider keys, …).
[ -f "$WS/agent-launchers/secrets.env" ] && set -a && . "$WS/agent-launchers/secrets.env" && set +a

cd "$WS"
exec "$ORCH_PY" "$WS/qwen-worker-watchdog.py" "$@"
