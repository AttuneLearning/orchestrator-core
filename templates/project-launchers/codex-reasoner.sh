#!/usr/bin/env bash
# Codex reasoner adapter for the orchestrator's CliReasoner.
#
# The CliReasoner runs `REASONER_CLI_CMD` with the prompt substituted for
# {prompt} and parses STDOUT as the model response (like `claude -p "{prompt}"`).
# `codex exec` streams session scaffolding to stdout, so we run it with
# --output-last-message and emit ONLY that final message on stdout.
#
# Model: uses codex's configured default (gpt-5.4-mini here); override with
# CODEX_REASONER_MODEL to pin a stronger model for gate decisions.
set -euo pipefail

prompt="${1:?codex-reasoner: prompt argument required}"
out="$(mktemp "${TMPDIR:-/tmp}/codex-reasoner-XXXXXX.txt")"
trap 'rm -f "$out"' EXIT

args=(exec --ephemeral --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -o "$out")
if [ -n "${CODEX_REASONER_MODEL:-}" ]; then
  args+=(-m "$CODEX_REASONER_MODEL")
fi

# stdin from /dev/null (codex exec otherwise blocks reading stdin); scaffolding +
# agent trace to /dev/null; only the final message reaches $out.
codex "${args[@]}" "$prompt" </dev/null >/dev/null 2>&1

cat "$out"
