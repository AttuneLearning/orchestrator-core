#!/usr/bin/env bash
# Restart the __PROJECT_NAME__ orchestrator engine (daemon).
#
# The engine's gate/triage/decompose decisions run as a DIRECT OpenAI-compatible
# API call (REASONER=openai) against the DigitalOcean inference endpoint, using the
# engine_reasoner model from config/instances.yaml (deepseek-4-flash, fallback
# deepseek-v4-pro). This is ~1s/call vs the old codex-CLI path's ~5-75s per-call
# boot tax. The DO profile auto-resolves base_url + MODEL_ACCESS_KEY.
#
# To A/B a different DO model without editing config: REASONER_MODEL=openai-gpt-oss-20b ./start-orch-daemon.sh
set -euo pipefail

WS="$(cd "$(dirname "$0")" && pwd)"
ORCH="__ORCH_PATH__"
LOG="/tmp/__PROJECT_NAME__-daemon.log"
INTERVAL="${1:-5}"

# Ensure the DO inference key is present for the reasoner (idempotent).
[ -f "$WS/agent-launchers/secrets.env" ] && set -a && . "$WS/agent-launchers/secrets.env" && set +a

if [ -z "${MODEL_ACCESS_KEY:-}" ]; then
  echo "ERROR: MODEL_ACCESS_KEY is unset (expected in $WS/agent-launchers/secrets.env)" >&2
  echo "The engine reasoner (REASONER=openai / DO deepseek) will 401 without it." >&2
  exit 1
fi

# Stop any running __PROJECT_NAME__ daemon first (single-writer).
pkill -f "orchestrator.cli --instance __PROJECT_NAME__ run --daemon" 2>/dev/null || true
sleep 1

export ORCH_INSTANCE=__PROJECT_NAME__
export REASONER=openai   # direct DO API (config engine_reasoner drives base_url/model/key)
# Local qwen dev workers run long, heads-down cycles (slow local inference + large
# context + code+tests) and can't emit a heartbeat mid-run. Give them a 30-min
# stale window (vs the global 900s) so they aren't falsely marked offline and
# churned through reclaim while legitimately working. Instance-scoped: this env
# only affects the __PROJECT_NAME__ daemon and agent_stale_seconds.
export AGENT_STALE_SECONDS="${AGENT_STALE_SECONDS:-1800}"

cd "$ORCH"
setsid .venv/bin/python -m orchestrator.cli --instance __PROJECT_NAME__ run \
  --daemon --interval "$INTERVAL" >"$LOG" 2>&1 </dev/null &
echo "started __PROJECT_NAME__ daemon (pid $!), reasoner=openai(DO/deepseek-4-flash), interval=${INTERVAL}s"
echo "log: $LOG"
