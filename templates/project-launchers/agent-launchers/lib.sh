#!/usr/bin/env bash

render_prompt() {
  local prompt_file="$1"
  if [ ! -f "$prompt_file" ]; then
    echo "missing prompt file: $prompt_file" >&2
    return 1
  fi
  ROLE="$ROLE" AGENT_ID="${AGENT_ID:-}" TEAM="${TEAM:-}" FUNCTION="${FUNCTION:-}" \
    GATE="${GATE:-}" APP="${APP:-}" PROJECT="$PROJECT" WORKTREE="$WORKTREE" \
    FANOUT="${FANOUT:-${FANOUT_DEFAULT:-3}}" \
    perl -pe 's/\{\{([A-Z0-9_]+)\}\}/exists $ENV{$1} ? $ENV{$1} : ""/ge' "$prompt_file"
}

enable_agent_loop() {
  if [ -z "${AGENT_ID:-}" ] || [ "${AGENT_ENABLE_LOOP:-${AGENT_ENABLE_LOOP_DEFAULT:-1}}" != "1" ]; then
    return 0
  fi
  PYTHONPATH="$ORCH${PYTHONPATH:+:$PYTHONPATH}" ORCH_INSTANCE="$PROJECT" \
    "$ORCH/.venv/bin/python" -m orchestrator.cli --instance "$PROJECT" \
    agent-loop --agent "$AGENT_ID" --enable >/dev/null || {
      echo "warning: could not enable dashboard loop for agent $AGENT_ID" >&2
      return 0
    }
}

print_launch_summary() {
  cat <<EOF
role=$ROLE
runtime=$RUNTIME
workspace=$WORKSPACE_ROOT
worktree=$WORKTREE
project=$PROJECT
orchestrator=$ORCH
dashboard=$DASHBOARD
agent_id=${AGENT_ID:-}
team=${TEAM:-}
function=${FUNCTION:-}
gate=${GATE:-}
app=${APP:-}
prompt=$PROMPT_FILE
loop_agent=$LOOP_AGENT
EOF
}
