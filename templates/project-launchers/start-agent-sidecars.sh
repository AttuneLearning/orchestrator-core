#!/usr/bin/env bash
# One-command fleet bring-up (durable-worker side-car edition), per-role runtimes.
#
# Brings up the pull fleet under AGENT_SIDECAR=1 in the "__PROJECT_NAME__" tmux
# session. Session + window names match the dashboard's contract (dashboard/app.py
# _tmux_session = project key; _worker_window = {be,fe,sr}-{dev,qa}), so every agent
# is live at  http://localhost:8800/workers?project=__PROJECT_NAME__
#
# ORCH-SAFE: this NEVER creates/kills the tmux session as a whole and NEVER touches
# window 0 (orch) — the orch-manager owns that window and killing it would take down
# the running orchestrator session. It only (re)builds the AGENT windows by name and
# leaves orch alone. If the session doesn't exist yet (fresh project) it creates it
# with an orch console window; if it exists, it reuses it.
#
#   ./start-agent-sidecars.sh              # (re)build the agent windows + launch the fleet
#   ./start-agent-sidecars.sh --dry-run    # print the plan, do nothing
#   ./start-agent-sidecars.sh --with-senior# also (re)build/launch sr-dev / sr-qa
#   ./start-agent-sidecars.sh --lane-2     # also the 2nd backend dev lane (agent 8)
#   ./start-agent-sidecars.sh kill         # tear down agent windows + drivers (KEEPS orch)
#   ./start-agent-sidecars.sh attach
#   restart == start (agent windows are rebuilt every run)
#
# PER-ROLE RUNTIME (edit to taste):
#   opencode       -> side-car owns a headless `opencode serve`; whole agent in ONE
#                     window (its log is what the dashboard tails).
#   claude / codex -> side-car drives a TUI. The TUI lives in the agent's window
#                     (dashboard tails the TUI); the side-car DRIVER runs as a
#                     background process (SIDECAR_TMUX_TARGET -> that window),
#                     logging to .sidecar-logs/<win>.log. `respawn-pane -k` means the
#                     driver must NOT share the agent's pane — hence the bg process.
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

# Create the session (with an orch console window 0) ONLY if it doesn't exist.
# Never touch window 0 of an existing session — that's the orch-manager's.
_ensure_session() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    [ "$DRY" = "1" ] && echo "  session '$SESSION' exists — reuse; window 0 (orch) left untouched"
    return
  fi
  if [ "$DRY" = "1" ]; then
    echo "  session '$SESSION' missing — create it with window 0 (orch console)"
    return
  fi
  tmux new-session -d -s "$SESSION" -n orch -c "$WS"
  tmux send-keys -t "$SESSION:orch" "clear; echo '=== orch ($SESSION) — orchestrator console ==='" C-m
}

# reap a background TUI driver for a window. Matches the driver's REAL argv
# (`sidecar.py … --tmux-target <session>:<name>.`) — the SIDECAR_TMUX_TARGET env
# var is NOT in the exec'd python's argv. The [s]idecar bracket keeps this pkill
# pattern from ever matching its OWN command line (pkill -f self-match footgun).
_reap_driver() { pkill -f "[s]idecar\\.py.*--tmux-target $SESSION:$1\\." 2>/dev/null || true; }

# remove ALL windows with a given name, addressed by unambiguous window-id —
# `kill-window -t session:name` is ambiguous (errors) when names are duplicated.
_drop_window() {
  local wid
  for wid in $(tmux list-windows -t "$SESSION" -F '#{window_id} #{window_name}' 2>/dev/null \
               | awk -v n="$1" '$2==n{print $1}'); do
    tmux kill-window -t "$wid" 2>/dev/null || true
  done
}

# _agent <name> <worktree_subdir> <launcher> <launch_team_arg> <runtime>
# console = on-demand: create the banner window only if missing, never kill a live one.
# opencode = side-car in-window. claude|codex = TUI in-window + bg driver.
_agent() {
  local name="$1" dir="$2" launcher="$3" team="$4" rt="$5"
  local path="$WS/$dir"
  local sc="AGENT_SIDECAR=1 \"$WS/$launcher\" $team $rt"

  if [ "$DRY" = "1" ]; then
    case "$rt" in
      console)  printf '  %-9s  %-16s  console (on-demand; created only if missing)\n' "$name" "$dir" ;;
      opencode) printf '  %-9s  %-16s  opencode in-window: %s\n' "$name" "$dir" "$sc" ;;
      *) printf '  %-9s  %-16s  %s TUI + bg driver (→ .sidecar-logs/%s.log), SIDECAR_TMUX_TARGET=%s:%s.0\n' \
           "$name" "$dir" "$rt" "$name" "$SESSION" "$name" ;;
    esac
    return
  fi

  if [ "$rt" = "console" ]; then
    tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$name" && return
    tmux new-window -t "$SESSION" -n "$name" -c "$path"
    tmux send-keys -t "$SESSION:$name" \
      "clear; echo '=== $name ($SESSION) — on-demand; launch with $WS/start-agent.sh ==='" C-m
    return
  fi

  # agent runtime: rebuild the window cleanly
  _reap_driver "$name"
  _drop_window "$name"
  tmux new-window -t "$SESSION" -n "$name" -c "$path"
  # remain-on-exit: if the TUI process exits (e.g. codex self-exits after a
  # first-run auto-update, or any crash), keep the pane as a DEAD pane the
  # side-car's respawn-pane can revive — instead of tmux closing the window,
  # which would strand the driver on "can't find window" MARKER_ERRORs.
  tmux set-window-option -t "$SESSION:$name" remain-on-exit on 2>/dev/null || true
  case "$rt" in
    opencode)
      tmux send-keys -t "$SESSION:$name" "$sc" C-m
      ;;
    claude|codex)
      tmux send-keys -t "$SESSION:$name" \
        "clear; echo '=== $name: $rt TUI — side-car driver starting (log: .sidecar-logs/$name.log) ==='" C-m
      mkdir -p "$SC_LOG_DIR"
      setsid env SIDECAR_TMUX_TARGET="$SESSION:$name.0" bash -lc "$sc" \
        >"$SC_LOG_DIR/$name.log" 2>&1 < /dev/null &
      ;;
    *) echo "unknown runtime '$rt' for $name" >&2; exit 2 ;;
  esac
}

plan() {
  _ensure_session
  _agent be-dev "wt-backend-dev"    start-dev-worker.sh backend  "$BE_DEV_RUNTIME"
  _agent be-qa  "wt-backend-qa"     start-qa-worker.sh  backend  "$BE_QA_RUNTIME"
  _agent fe-dev "wt-frontend-dev"   start-dev-worker.sh frontend "$FE_DEV_RUNTIME"
  _agent fe-qa  "wt-frontend-qa"    start-qa-worker.sh  frontend "$FE_QA_RUNTIME"
  if [ "$WITH_SENIOR" = "1" ]; then
    _agent sr-dev "wt-senior-dev"   start-senior-dev.sh ""       "$SR_DEV_RUNTIME"
    _agent sr-qa  "wt-senior-qa"    start-senior-qa.sh  ""       "$SR_QA_RUNTIME"
  else
    _agent sr-dev "wt-senior-dev"   -            -        console
    _agent sr-qa  "wt-senior-qa"    -            -        console
  fi
  [ "$LANE_2" = "1" ] && _agent be-dev-2 "wt-backend-dev-2" start-dev-worker.sh backend-2 "$BE_DEV_2_RUNTIME"
}

start() {
  if [ "$DRY" = "1" ]; then
    echo "PLAN (session '$SESSION', orch window 0 untouched):"
    plan
    echo "(dry-run — nothing created or launched)"
    return
  fi
  plan
  echo "fleet up in session '$SESSION' (orch window preserved)."
  echo "Runtimes: be-dev=$BE_DEV_RUNTIME be-qa=$BE_QA_RUNTIME fe-dev=$FE_DEV_RUNTIME fe-qa=$FE_QA_RUNTIME"
  echo "Dashboard: http://localhost:8800/workers?project=$SESSION"
  echo "Attach:    tmux attach -t $SESSION    TUI driver logs: $SC_LOG_DIR/<win>.log"
}

teardown_agents() {
  for w in be-dev be-qa fe-dev fe-qa sr-dev sr-qa be-dev-2; do
    _reap_driver "$w"; _drop_window "$w"
  done
  # catch-all: any remaining TUI driver bound to this session (bracket = no self-match)
  pkill -f "[s]idecar\\.py.*--tmux-target $SESSION:" 2>/dev/null || true
  echo "agent windows + drivers torn down (session '$SESSION' and orch window kept)."
}

case "$CMD" in
  start|restart) start ;;
  attach)        tmux attach -t "$SESSION" ;;
  kill)          teardown_agents ;;
esac
