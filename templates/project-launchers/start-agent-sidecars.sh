#!/usr/bin/env bash
# One-command fleet bring-up (durable-worker side-car edition).
#
# Creates the "__PROJECT_NAME__" tmux session with one window per agent AND
# auto-launches each pull worker under AGENT_SIDECAR=1. Unlike start-agent-sessions.sh
# (which only lays out the windows and leaves you to launch each agent by hand),
# this starts the whole standing fleet in one command.
#
# The session name and window names match the dashboard's contract exactly
# (dashboard/app.py _tmux_session = project key; _worker_window = {be,fe,sr}-{dev,qa}),
# so every launched agent is live-visible under the dashboard's /workers page
# (http://localhost:8800/workers?project=__PROJECT_NAME__).
#
#   ./start-agent-sidecars.sh              # create session + launch the pull fleet as side-cars
#   ./start-agent-sidecars.sh --dry-run    # print the plan (windows + launch commands), do nothing
#   ./start-agent-sidecars.sh --with-senior# also launch sr-dev / sr-qa (default: on-demand only)
#   ./start-agent-sidecars.sh --lane-2     # also launch the second backend dev lane (agent 8)
#   ./start-agent-sidecars.sh attach|kill|restart
#
# Runtime is opencode — the only one that fits one-window-per-agent (the side-car
# owns a headless `opencode serve`, so the whole agent lives in ONE window whose
# log the dashboard tails). claude/codex side-car mode drives a TUI in a SEPARATE
# pane (needs SIDECAR_TMUX_TARGET), so launch those by hand with start-agent.sh.
# RUNTIME is fixed here (NOT read from the environment) so an ambient RUNTIME from
# an orch-manager shell can't hijack the fleet's runtime.
set -u
SESSION=__PROJECT_NAME__
WS=__WORKSPACE_ROOT__
RUNTIME=opencode
DRY=0
WITH_SENIOR=0
LANE_2=0
CMD=start

for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY=1 ;;
    --with-senior) WITH_SENIOR=1 ;;
    --lane-2)      LANE_2=1 ;;
    start|attach|kill|restart) CMD="$arg" ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# win_index | window_name | worktree_subdir | launch_command ("" = console/banner only)
# Launch commands run in the window foreground; the window persists on detach, so
# the side-car keeps ticking. Ctrl-C in a window stops just that one agent.
_win() {
  local idx="$1" name="$2" dir="$3" cmd="$4"
  local path="$WS/$dir"
  if [ "$DRY" = "1" ]; then
    printf '  win %s  %-9s  %-18s  %s\n' "$idx" "$name" "$dir" "${cmd:-<console/banner>}"
    return
  fi
  if [ "$idx" -eq 0 ]; then
    tmux new-session -d -s "$SESSION" -n "$name" -c "$path"
  else
    tmux new-window -t "$SESSION:$idx" -n "$name" -c "$path"
  fi
  if [ -n "$cmd" ]; then
    tmux send-keys -t "$SESSION:$idx" "$cmd" C-m
  else
    tmux send-keys -t "$SESSION:$idx" \
      "clear; echo '=== $name ($SESSION) — on-demand; launch with $WS/start-agent.sh ==='" C-m
  fi
}

# side-car launch line for a pull worker
_sc() { echo "AGENT_SIDECAR=1 \"$WS/$1\" $2 $RUNTIME"; }

plan() {
  _win 0 orch       "."                ""
  _win 1 be-dev     "wt-backend-dev"   "$(_sc start-dev-worker.sh backend)"
  _win 2 be-qa      "wt-backend-qa"    "$(_sc start-qa-worker.sh backend)"
  _win 3 fe-dev     "wt-frontend-dev"  "$(_sc start-dev-worker.sh frontend)"
  _win 4 fe-qa      "wt-frontend-qa"   "$(_sc start-qa-worker.sh frontend)"
  if [ "$WITH_SENIOR" = "1" ]; then
    _win 5 sr-dev   "wt-senior-dev"    "AGENT_SIDECAR=1 \"$WS/start-senior-dev.sh\" $RUNTIME"
    _win 6 sr-qa    "wt-senior-qa"     "AGENT_SIDECAR=1 \"$WS/start-senior-qa.sh\" $RUNTIME"
  else
    _win 5 sr-dev   "wt-senior-dev"    ""
    _win 6 sr-qa    "wt-senior-qa"     ""
  fi
  [ "$LANE_2" = "1" ] && _win 7 be-dev-2 "wt-backend-dev-2" \
    "AGENT_SIDECAR=1 \"$WS/start-dev-worker.sh\" backend-2 $RUNTIME"
}

start() {
  if [ "$DRY" = "1" ]; then
    echo "PLAN (session '$SESSION', runtime '$RUNTIME'):"
    plan
    echo "(dry-run — nothing created or launched)"
    return
  fi
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "session '$SESSION' already running — leaving it (attach: tmux attach -t $SESSION)." >&2
    echo "to rebuild:  $0 restart" >&2
    exit 1
  fi
  plan
  tmux select-window -t "$SESSION:0"
  echo "session '$SESSION' up — pull fleet launched as $RUNTIME side-cars."
  echo "Dashboard:  http://localhost:8800/workers?project=$SESSION"
  echo "Attach:     tmux attach -t $SESSION    (Ctrl-b <n> switch, Ctrl-b d detach)"
}

case "$CMD" in
  start)   start ;;
  attach)  tmux attach -t "$SESSION" ;;
  kill)    tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed '$SESSION'" || echo "no session '$SESSION'" ;;
  restart) tmux kill-session -t "$SESSION" 2>/dev/null; DRY=0; start ;;
esac
