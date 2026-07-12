#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$WS/start-opencode.sh" -m "orch_model/glm-5.1" "$@"
