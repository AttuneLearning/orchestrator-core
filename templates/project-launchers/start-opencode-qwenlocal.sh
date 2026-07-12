#!/usr/bin/env bash
set -euo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$WS/start-opencode.sh" -m "qwen_local/qwen-local" "$@"
