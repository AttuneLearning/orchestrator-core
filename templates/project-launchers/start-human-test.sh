#!/usr/bin/env bash
# Start/stop the human-test environment in a detached tmux session.
#   ./start-human-test.sh start   - (re)launch API :5161 + web :5174 in tmux, auto-restart on crash
#   ./start-human-test.sh stop    - stop the session and both servers
#   ./start-human-test.sh status  - show what's up
#   ./start-human-test.sh attach  - attach to the tmux session (Ctrl-b d to detach)
# Maintained by the orchestrator launcher kit (install-launchers / setup-project).
# Survives terminal close (tmux). For reboot-survival: sudo loginctl enable-linger <user>.
set -u
SESSION=human-test
WS=__WORKSPACE_ROOT__

# The human-test worktree name varies by project (wt-human / wt-human-test /
# humantest-wt). Auto-detect the first that exists under the workspace; override
# with HUMANTEST_WT=<dir> if yours differs.
WT="${HUMANTEST_WT:-}"
if [ -z "$WT" ]; then
  for cand in wt-human wt-human-test humantest-wt; do
    [ -d "$WS/$cand" ] && { WT="$WS/$cand"; break; }
  done
fi
: "${WT:=$WS/wt-human}"
INFRA="$WS/.human-test-infra/test-infra.sh"

_kill_port() { # free a TCP port
  local p="$1" pid
  pid=$(ss -ltnp 2>/dev/null | grep ":$p " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
}

start() {
  if [ ! -d "$WT" ]; then
    echo "human-test worktree not found under $WS (looked for wt-human/wt-human-test/humantest-wt)." >&2
    echo "set HUMANTEST_WT=<dir> or create the worktree first." >&2
    exit 1
  fi
  # 1) isolated test Mongo/Redis (no-op if the infra script is absent)
  [ -x "$INFRA" ] && "$INFRA" start
  # 2) clear stale app procs (orphaned ts-node-dev / vite from prior foreground runs)
  pkill -9 -f "$WT/apps/api" 2>/dev/null
  pkill -9 -f "$WT.*vite"    2>/dev/null
  _kill_port 5161; _kill_port 5174
  sleep 1
  # 3) fresh tmux session, one window per server, each in an auto-restart loop.
  #    Window 0 = api (backend), window 1 = web (frontend). Switch with Ctrl-b 0/1.
  tmux kill-session -t "$SESSION" 2>/dev/null
  tmux new-session -d -s "$SESSION" -n api -c "$WT" \
    "while true; do npm run -w apps/api dev; echo '[api exited — restarting in 2s]'; sleep 2; done"
  tmux new-window -t "$SESSION" -n web -c "$WT" \
    "while true; do npm run -w apps/web dev; echo '[web exited — restarting in 2s]'; sleep 2; done"
  tmux select-window -t "$SESSION":api
  echo "human-test launched in tmux session '$SESSION' from $WT (API :5161, web :5174)."
  echo "Watch logs:  tmux attach -t $SESSION   (Ctrl-b 0/1 switch, Ctrl-b d detach)"
}

stop() {
  tmux kill-session -t "$SESSION" 2>/dev/null && echo "tmux session stopped."
  pkill -9 -f "$WT/apps/api" 2>/dev/null
  pkill -9 -f "$WT.*vite"    2>/dev/null
  _kill_port 5161; _kill_port 5174
  echo "servers stopped."
}

status() {
  tmux has-session -t "$SESSION" 2>/dev/null && echo "tmux session '$SESSION': UP" || echo "tmux session '$SESSION': down"
  echo "worktree: $WT"
  for p in 5161 5174; do
    ss -ltn 2>/dev/null | grep -q ":$p " && echo "  :$p listening" || echo "  :$p down"
  done
  [ -x "$INFRA" ] && "$INFRA" status
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  attach) tmux attach -t "$SESSION" ;;
  *) echo "usage: $0 {start|stop|status|attach}"; exit 1 ;;
esac
