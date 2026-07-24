#!/usr/bin/env bash
# One-command fleet bring-up (durable-worker side-car edition), per-role runtimes.
#
# Creates the "__PROJECT_NAME__" tmux session with one window per agent AND
# auto-launches the pull fleet under AGENT_SIDECAR=1. Session + window names match
# the dashboard's contract (dashboard/app.py _tmux_session = project key;
# _worker_window = {be,fe,sr}-{dev,qa}), so every agent is live at
#   http://localhost:8800/workers?project=__PROJECT_NAME__
#
#   ./start-agent-sidecars.sh              # create session + launch the pull fleet
#   ./start-agent-sidecars.sh --dry-run    # print the plan, do nothing
#   ./start-agent-sidecars.sh --with-senior# also launch sr-dev / sr-qa
#   ./start-agent-sidecars.sh --lane-2     # also launch the 2nd backend dev lane (agent 8)
#   ./start-agent-sidecars.sh attach|kill|restart
#
# PER-ROLE RUNTIME (edit these to taste):
#   opencode -> the side-car owns a headless `opencode serve`; the whole agent
#               lives in ONE window (its log is what the dashboard tails).
#   claude / codex -> the side-car drives a TUI via tmux. The TUI lives in the
#               agent's window (dashboard tails the TUI); the side-car DRIVER runs
#               as a background process (SIDECAR_TMUX_TARGET -> that window),
#               logging to .sidecar-logs/<win>.log. `respawn-pane -k` means the
#               driver must NOT share the agent's pane — hence the bg process.
BE_DEV_RUNTIME=claude
FE_DEV_RUNTIME=claude
BE_QA_RUNTIME=codex
FE_QA_RUNTIME=codex
SR_DEV_RUNTIME=claude
SR_QA_RUNTIME=codex
BE_DEV_2_RUNTIME=opencode

set -u
SESSION=__PROJECT_NAME__
WS=__WORKSPACE_ROOT__
SC_LOG_DIR="$WS/.sidecar-logs"
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

# _agent <idx> <name> <worktree_subdir> <launcher> <launch_team_arg> <runtime>
# runtime "console" = create the window with a banner, launch nothing (orch / on-demand).
# opencode        = run the side-car IN the window (log visible to the dashboard).
# claude|codex    = window hosts the TUI (dashboard tails it); driver runs in the
#                   background with SIDECAR_TMUX_TARGET pointed at this window.
_agent() {
  local idx="$1" name="$2" dir="$3" launcher="$4" team="$5" rt="$6"
  local path="$WS/$dir"
  local sc="AGENT_SIDECAR=1 \"$WS/$launcher\" $team $rt"

  if [ "$DRY" = "1" ]; then
    case "$rt" in
      console) printf '  win %s  %-9s  %-16s  console (banner; launch on demand)\n' "$idx" "$name" "$dir" ;;
      opencode) printf '  win %s  %-9s  %-16s  opencode in-window: %s\n' "$idx" "$name" "$dir" "$sc" ;;
      *) printf '  win %s  %-9s  %-16s  %s TUI + bg driver (→ .sidecar-logs/%s.log): SIDECAR_TMUX_TARGET=%s:%s.0 %s\n' \
           "$idx" "$name" "$dir" "$rt" "$name" "$SESSION" "$name" "$sc" ;;
    esac
    return
  fi

  if [ "$idx" -eq 0 ]; then
    tmux new-session -d -s "$SESSION" -n "$name" -c "$path"
  else
    tmux new-window -t "$SESSION:$idx" -n "$name" -c "$path"
  fi

  case "$rt" in
    console)
      tmux send-keys -t "$SESSION:$idx" \
        "clear; echo '=== $name ($SESSION) — on-demand; launch with $WS/start-agent.sh ==='" C-m
      ;;
    opencode)
      tmux send-keys -t "$SESSION:$idx" "$sc" C-m
      ;;
    claude|codex)
      tmux send-keys -t "$SESSION:$idx" \
        "clear; echo '=== $name: $rt TUI — side-car driver starting (log: .sidecar-logs/$name.log) ==='" C-m
      mkdir -p "$SC_LOG_DIR"
      # Driver runs detached; it respawn-panes the TUI into THIS window and drives
      # it. It must not live in the window it kills, so it is a bg process, not a pane.
      setsid env SIDECAR_TMUX_TARGET="$SESSION:$name.0" bash -lc "$sc" \
        >"$SC_LOG_DIR/$name.log" 2>&1 < /dev/null &
      ;;
    *) echo "unknown runtime '$rt' for $name" >&2; exit 2 ;;
  esac
}

plan() {
  _agent 0 orch   "."                 -            -        console
  _agent 1 be-dev "wt-backend-dev"    start-dev-worker.sh backend  "$BE_DEV_RUNTIME"
  _agent 2 be-qa  "wt-backend-qa"     start-qa-worker.sh  backend  "$BE_QA_RUNTIME"
  _agent 3 fe-dev "wt-frontend-dev"   start-dev-worker.sh frontend "$FE_DEV_RUNTIME"
  _agent 4 fe-qa  "wt-frontend-qa"    start-qa-worker.sh  frontend "$FE_QA_RUNTIME"
  if [ "$WITH_SENIOR" = "1" ]; then
    _agent 5 sr-dev "wt-senior-dev"   start-senior-dev.sh ""       "$SR_DEV_RUNTIME"
    _agent 6 sr-qa  "wt-senior-qa"    start-senior-qa.sh  ""       "$SR_QA_RUNTIME"
  else
    _agent 5 sr-dev "wt-senior-dev"   -            -        console
    _agent 6 sr-qa  "wt-senior-qa"    -            -        console
  fi
  [ "$LANE_2" = "1" ] && _agent 7 be-dev-2 "wt-backend-dev-2" start-dev-worker.sh backend-2 "$BE_DEV_2_RUNTIME"
}

start() {
  if [ "$DRY" = "1" ]; then
    echo "PLAN (session '$SESSION'):"
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
  echo "session '$SESSION' up."
  echo "Runtimes: be-dev=$BE_DEV_RUNTIME be-qa=$BE_QA_RUNTIME fe-dev=$FE_DEV_RUNTIME fe-qa=$FE_QA_RUNTIME"
  echo "Dashboard: http://localhost:8800/workers?project=$SESSION"
  echo "Attach:    tmux attach -t $SESSION   (Ctrl-b <n> switch, Ctrl-b d detach)"
  echo "TUI driver logs: $SC_LOG_DIR/<win>.log"
}

case "$CMD" in
  start)   start ;;
  attach)  tmux attach -t "$SESSION" ;;
  kill)
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed session '$SESSION'" || echo "no session '$SESSION'"
    # stop any background TUI drivers this script started
    pkill -f "SIDECAR_TMUX_TARGET=$SESSION:" 2>/dev/null || true
    ;;
  restart)
    tmux kill-session -t "$SESSION" 2>/dev/null
    pkill -f "SIDECAR_TMUX_TARGET=$SESSION:" 2>/dev/null || true
    DRY=0; start
    ;;
esac
