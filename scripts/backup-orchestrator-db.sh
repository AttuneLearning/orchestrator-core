#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ORCH_ROOT}"

INSTANCE="${ORCH_INSTANCE:-}"
REASON="${1:-manual}"

if [ -n "${INSTANCE}" ]; then
  exec "${ORCH_ROOT}/.venv/bin/python" -m orchestrator.cli --instance "${INSTANCE}" backup-db --reason "${REASON}"
fi

exec "${ORCH_ROOT}/.venv/bin/python" -m orchestrator.cli backup-db --reason "${REASON}"
