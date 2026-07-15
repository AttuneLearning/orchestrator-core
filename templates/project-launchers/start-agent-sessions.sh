#!/usr/bin/env bash
# Start ONE tmux session ("__PROJECT_NAME__") with 7 WINDOWS — one per role — each cd'd
# into the right worktree and showing a banner (identity + launch command). Attach
# once and switch windows with Ctrl-b <n>. It does NOT start the CLI agents; you
# launch them yourself in each window.
#
#   ./start-agent-sessions.sh          # create the session if missing (won't touch a running one)
#   ./start-agent-sessions.sh attach   # attach to it
#   ./start-agent-sessions.sh kill      # kill the session
#
# Windows (Ctrl-b then the number):
#   0 orch     orch-manager        ./ (workspace root)   — dashboard / daemon / monitor
#   1 be-dev   backend  dev  (a1)  wt-backend-dev    -> $WS/start-dev-worker.sh backend
#   2 be-qa    backend  qa   (a2)  wt-backend-qa     -> $WS/start-qa-worker.sh backend
#   3 fe-dev   frontend dev  (a3)  wt-frontend-dev   -> $WS/start-dev-worker.sh frontend
#   4 fe-qa    frontend qa   (a4)  wt-frontend-qa    -> $WS/start-qa-worker.sh frontend
#   5 sr-dev   senior   dev  (a5)  wt-senior-dev     -> $WS/start-senior-dev.sh (on escalation)
#   6 sr-qa    senior   qa   (a6)  wt-senior-qa      -> $WS/start-senior-qa.sh (on escalation)
set -u
SESSION=__PROJECT_NAME__
WS=__WORKSPACE_ROOT__
ORCH=__ORCH_PATH__
BAN="$WS/.agent-banners"

write_banners() {
  mkdir -p "$BAN"
  cat > "$BAN/orch.txt" <<EOF
════ ORCH-MANAGER (__PROJECT_NAME__) ════
Dashboard : http://localhost:8800/?project=__PROJECT_NAME__
Daemon    : (from $ORCH)
            DATABASE_URL=<project DATABASE_URL> \\
            ROSTER_FILE=config/roster.__PROJECT_NAME__.yaml \\
            setsid .venv/bin/python -m orchestrator.cli run --daemon --interval 5 &
Monitor   : DATABASE_URL=<project DATABASE_URL> \\
            PYTHONPATH=$ORCH .venv/bin/python $WS/.orch-monitor-problems.py 0
EOF
  cat > "$BAN/be-dev.txt" <<EOF
════ BACKEND DEV — agent 1 (Qwen Code) ════
Worktree : wt-backend-dev   Coordinator: __PROJECT_NAME__ (serve --instance __PROJECT_NAME__)
Launch   : $WS/start-dev-worker.sh backend  # add --issue <id> to pin a task
EOF
  cat > "$BAN/be-qa.txt" <<EOF
════ BACKEND QA — agent 2 (Codex) ════
Worktree : wt-backend-qa    Coordinator: __PROJECT_NAME__
Launch   : $WS/start-qa-worker.sh backend
EOF
  cat > "$BAN/fe-dev.txt" <<EOF
════ FRONTEND DEV — agent 3 (Qwen Code) ════
Worktree : wt-frontend-dev  Coordinator: __PROJECT_NAME__ (serve --instance __PROJECT_NAME__)
Launch   : $WS/start-dev-worker.sh frontend # add --issue <id> to pin a task
EOF
  cat > "$BAN/fe-qa.txt" <<EOF
════ FRONTEND QA — agent 4 (Codex) ════
Worktree : wt-frontend-qa   Coordinator: __PROJECT_NAME__
Launch   : $WS/start-qa-worker.sh frontend
EOF
  cat > "$BAN/sr-dev.txt" <<EOF
════ SENIOR / ESCALATION DEV — agent 5 (Claude Code) ════
Worktree : wt-senior-dev    Coordinator: __PROJECT_NAME__ (via ./.mcp.json)
Launch   : $WS/start-senior-dev.sh          # ON DEMAND, after escalating an issue to agent 5
Role     : cross-team; reads the escalated issue's team -> apps/api | apps/web | packages/contracts
EOF
  cat > "$BAN/sr-qa.txt" <<EOF
════ SENIOR / ESCALATION QA — agent 6 (Claude Code) ════
Worktree : wt-senior-qa     Coordinator: __PROJECT_NAME__ (via ./.mcp.json)
Launch   : $WS/start-senior-qa.sh           # ON DEMAND, after escalating an issue to agent 6
Role     : cross-team verification; reads the escalated issue's team -> apps/api | apps/web | packages/contracts
EOF
}

# win_index | window_name | dir | banner_key
_win() {
  local idx="$1" name="$2" dir="$3" ban="$4"
  if [ "$idx" -eq 0 ]; then
    tmux new-session -d -s "$SESSION" -n "$name" -c "$dir"
  else
    tmux new-window -t "$SESSION:$idx" -n "$name" -c "$dir"
  fi
  tmux send-keys -t "$SESSION:$idx" "clear; cat '$BAN/$ban.txt'; echo" C-m
}

start() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "session '$SESSION' already running — leaving it (attach: tmux attach -t $SESSION)"
    return
  fi
  write_banners
  # clean up the older per-role sessions if they exist (superseded by this single session)
  for s in tc-orch tc-be-dev tc-be-qa tc-fe-dev tc-fe-qa; do tmux kill-session -t "$s" 2>/dev/null; done
  _win 0 orch   "$WS"                 orch
  _win 1 be-dev "$WS/wt-backend-dev"  be-dev
  _win 2 be-qa  "$WS/wt-backend-qa"   be-qa
  _win 3 fe-dev "$WS/wt-frontend-dev" fe-dev
  _win 4 fe-qa  "$WS/wt-frontend-qa"  fe-qa
  _win 5 sr-dev "$WS/wt-senior-dev"   sr-dev
  _win 6 sr-qa  "$WS/wt-senior-qa"    sr-qa
  tmux select-window -t "$SESSION:0"
  echo "session '$SESSION' created with 7 windows (0 orch, 1 be-dev, 2 be-qa, 3 fe-dev, 4 fe-qa, 5 sr-dev, 6 sr-qa)."
  echo "Attach:  tmux attach -t $SESSION      Switch windows: Ctrl-b <0-6>   Detach: Ctrl-b d"
}

case "${1:-start}" in
  start) start ;;
  attach) tmux attach -t "$SESSION" ;;
  kill) tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed session '$SESSION'" || echo "no session '$SESSION'" ;;
  *) echo "usage: $0 {start|attach|kill}"; exit 1 ;;
esac
