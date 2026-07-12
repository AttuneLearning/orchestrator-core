#!/usr/bin/env bash
# run-agent-loop.sh <agent_id> <cmd...>
#
# Poll loop for looping agents (QA workers, dev-manager, senior). Each iteration:
#   1. checks the agent's dashboard cooldown; if paused, sleeps and auto-resumes;
#   2. runs <cmd> ONCE (one work cycle — e.g. `opencode run …` / `codex exec …`
#      / `claude -p …`), streaming its output live while capturing it;
#   3. classifies any limit signal in the output:
#        - TRANSIENT (overload / 429 / timeout): retries the cycle up to
#          AGENT_RETRIES times with short escalating backoff;
#        - HARD (usage/quota cap): sets a 2h cooldown via the dashboard API (the
#          engine stops assigning to it; the loop sleeps until it lapses, then
#          auto-resumes). Only after retries are exhausted, or on a real cap.
#   4. sleeps AGENT_POLL seconds and repeats.
#
# Env: ORCH_DASHBOARD (default __DASHBOARD_URL__), AGENT_POLL (default 90),
#      AGENT_IDLE_STOP (>0 = exit after N consecutive NO WORK cycles),
#      AGENT_RETRIES (default 3), AGENT_RETRY_BASE (default 20s, ×attempt backoff).
set -u
AID="${1:?usage: run-agent-loop.sh <agent_id> <cmd...>}"; shift
DASH="${ORCH_DASHBOARD:-__DASHBOARD_URL__}"
PROJ=__PROJECT_NAME__
POLL="${AGENT_POLL:-90}"           # idle poll cadence (API usage is metered — don't spin fast)
IDLE_STOP="${AGENT_IDLE_STOP:-0}"  # >0: exit after this many consecutive NO WORK cycles (on-demand agents)
AGENT_RETRIES="${AGENT_RETRIES:-3}"         # transient-limit retries before falling back to a 2h pause
AGENT_RETRY_BASE="${AGENT_RETRY_BASE:-20}"  # base backoff seconds; wait = base × attempt
# TRANSIENT signals — short-lived overload/rate-limit/timeout that clears on its own;
# retry with backoff. HARD signals — a real usage/quota cap; cool down for 2h.
SOFT_RE='overloaded_error|\boverloaded\b|rate limit exceeded|429 too many requests|too many requests\b|\b503\b|service unavailable|\b529\b|connection error|request timed out|\btimeout\b'
HARD_RE='usage limit reached|hit your usage limit|approaching .*usage limit|resets at|insufficient_quota|quota exceeded|out of (credits|tokens)'

pause_secs() {
  curl -s -m 8 "$DASH/agents/$AID/pause?project=$PROJ" 2>/dev/null \
    | sed -n 's/.*"pause_seconds":\([0-9]\{1,\}\).*/\1/p'
}
set_pause() {  # minutes
  curl -s -m 8 -o /dev/null -X POST "$DASH/agents/pause?project=$PROJ" \
    --data "agent_id=$AID&minutes=$1"
}

# Durable per-agent log (survives tmux scrollback + temp cleanup): tail -f "$LOG".
LOGDIR="${AGENT_LOG_DIR:-$HOME/.cache/orch-agent-logs}"
mkdir -p "$LOGDIR" 2>/dev/null || true
LOG="$LOGDIR/agent-$AID.log"

echo "== agent $AID cooldown loop :: cmd = $* (retries=$AGENT_RETRIES) ==" | tee -a "$LOG"
echo "== durable log: $LOG =="
idle=0
soft=0   # consecutive transient-limit retries in the current streak
while true; do
  secs="$(pause_secs)"; secs="${secs:-0}"
  if [ "$secs" -gt 0 ] 2>/dev/null; then
    nap=$(( secs < 300 ? secs : 300 ))
    echo "== agent $AID PAUSED — ~${secs}s left; sleeping ${nap}s then re-checking (auto-resume) =="
    sleep "$nap"; continue
  fi
  echo "----- agent $AID cycle @ $(date '+%Y-%m-%d %H:%M:%S') -----" | tee -a "$LOG"
  tmpf="$(mktemp)"
  "$@" </dev/null 2>&1 | tee -a "$tmpf" "$LOG"

  # HARD usage/quota cap: retrying won't help -> long cooldown now.
  if grep -qiE "$HARD_RE" "$tmpf"; then
    echo "== agent $AID: usage/quota cap detected -> pausing 2h (auto-resume after cooldown) =="
    set_pause 120; soft=0
    rm -f "$tmpf"; continue
  fi
  # TRANSIENT overload/rate-limit/timeout: retry with short backoff before pausing.
  if grep -qiE "$SOFT_RE" "$tmpf"; then
    soft=$((soft + 1))
    if [ "$soft" -le "$AGENT_RETRIES" ]; then
      back=$(( AGENT_RETRY_BASE * soft ))
      echo "== agent $AID: transient limit -> retry $soft/$AGENT_RETRIES after ${back}s =="
      rm -f "$tmpf"; sleep "$back"; continue
    fi
    echo "== agent $AID: transient limit persisted after $AGENT_RETRIES retries -> pausing 2h =="
    set_pause 120; soft=0
    rm -f "$tmpf"; continue
  fi
  soft=0   # a clean cycle clears the retry streak

  if grep -qiE 'no work' "$tmpf"; then idle=$((idle + 1)); else idle=0; fi
  rm -f "$tmpf"
  if [ "$IDLE_STOP" -gt 0 ] && [ "$idle" -ge "$IDLE_STOP" ]; then
    echo "== agent $AID: $idle idle cycle(s) -> stopping (on-demand) =="; break
  fi
  sleep "$POLL"
done
