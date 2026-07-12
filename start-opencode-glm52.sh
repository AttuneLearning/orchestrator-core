#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v opencode >/dev/null 2>&1; then
  echo "missing opencode CLI on PATH" >&2
  exit 1
fi

if [ -z "${MODEL_ACCESS_KEY:-}" ]; then
  MODEL_ACCESS_KEY="${DO_API_KEY:-}"
fi
if [ -z "${MODEL_ACCESS_KEY:-}" ]; then
  echo "MODEL_ACCESS_KEY is required for DigitalOcean opencode access" >&2
  exit 2
fi
export MODEL_ACCESS_KEY

WORKTREE="${OPENCODE_WORKTREE:-}"
if [ -z "$WORKTREE" ]; then
  if [ -d "$SCRIPT_DIR/humantest-wt" ]; then
    WORKTREE="$SCRIPT_DIR/humantest-wt"
  elif [ -d "$SCRIPT_DIR/wt-human-test" ]; then
    WORKTREE="$SCRIPT_DIR/wt-human-test"
  else
    WORKTREE="$SCRIPT_DIR"
  fi
fi

tmpcfg="$(mktemp -d "${TMPDIR:-/tmp}/opencode-glm52.XXXXXX")"
cleanup() {
  rm -rf "$tmpcfg"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmpcfg/opencode"
cat > "$tmpcfg/opencode/opencode.jsonc" <<'JSONC'
{
  "$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["orch_model"],
  "provider": {
    "orch_model": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Orchestrator OpenAI-Compatible",
      "options": {
        "baseURL": "https://inference.do-ai.run/v1",
        "apiKey": "{env:MODEL_ACCESS_KEY}"
      },
      "models": {
        "glm-5.2": {
          "name": "GLM 5.2"
        }
      }
    }
  },
  "model": "orch_model/glm-5.2",
  "small_model": "orch_model/glm-5.2"
}
JSONC

export XDG_CONFIG_HOME="$tmpcfg"
cd "$WORKTREE"
exec opencode --model orch_model/glm-5.2 "$@"
