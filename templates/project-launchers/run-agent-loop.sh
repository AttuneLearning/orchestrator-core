#!/usr/bin/env bash
# run-agent-loop.sh <agent_id> <cmd...>
#
# Poll loop for looping agents. The loop's ON/OFF and idle cadence are OWNED BY
# THE DASHBOARD (the Agents page): each cycle re-reads the agent's
# `loop_enabled` + `poll_interval_seconds` from the coordinator and obeys them —
# no static env cadence. Each iteration:
#   1. reads the agent's dashboard state (pause + loop policy) in one request;
#      if paused, sleeps and auto-resumes;
#   2. runs <cmd> ONCE (one work cycle — e.g. `opencode run …` / `codex exec …`
#      / `claude -p …`);
#        - NON-INTERACTIVE (default): streams output live while capturing it, then
#          classifies any limit signal (see below) and runs the wedge detector;
#        - INTERACTIVE (AGENT_LOOP_INTERACTIVE=1 — a TUI): runs attached to the
#          terminal with NO stdin/stdout redirection, capture, per-cycle timeout,
#          or wedge detection (those are headless-only and would corrupt a TUI);
#   3. (non-interactive) classifies any limit signal in the output:
#        - TRANSIENT (overload / 429 / timeout): retries the cycle up to
#          AGENT_RETRIES times with short escalating backoff;
#        - HARD (usage/quota cap): sets a 2h cooldown via the dashboard API (the
#          engine stops assigning to it; the loop sleeps until it lapses, then
#          auto-resumes). Only after retries are exhausted, or on a real cap.
#   4. if the dashboard says loop_enabled=false, stops after this cycle (one-shot);
#   5. otherwise sleeps the dashboard's poll_interval_seconds and repeats.
#
# Env: ORCH_DASHBOARD (default __DASHBOARD_URL__),
#      AGENT_LOOP_INTERACTIVE (1 = TUI cycle; set by the runtime adapter),
#      AGENT_POLL (fallback idle cadence ONLY when the dashboard is unreachable /
#        returns no interval; default 90),
#      AGENT_IDLE_STOP (>0 = exit after N consecutive NO WORK cycles),
#      AGENT_RETRIES (default 3), AGENT_RETRY_BASE (default 20s, ×attempt backoff),
#      AGENT_CYCLE_TIMEOUT (default 1800s — GAP-3 hard cap per work cycle),
#      AGENT_WEDGE_REPEATS (default 3 — GAP-3: N consecutive identical-output
#      cycles = a wedged model looping; pause 2h instead of burning tokens),
#      AGENT_HEARTBEAT_SECONDS (default 20 — continuous liveness ping for the
#      lifetime of the loop, through inter-cycle sleeps; 0 disables it).
set -u
AID="${1:?usage: run-agent-loop.sh <agent_id> <cmd...>}"; shift
DASH="${ORCH_DASHBOARD:-__DASHBOARD_URL__}"
PROJ=__PROJECT_NAME__
POLL="${AGENT_POLL:-90}"           # fallback cadence only (dashboard interval wins)
INTERACTIVE="${AGENT_LOOP_INTERACTIVE:-0}"  # 1 = TUI cycle (no capture/classify/timeout)
IDLE_STOP="${AGENT_IDLE_STOP:-0}"  # >0: exit after this many consecutive NO WORK cycles (on-demand agents)
AGENT_RETRIES="${AGENT_RETRIES:-3}"         # transient-limit retries before falling back to a 2h pause
AGENT_RETRY_BASE="${AGENT_RETRY_BASE:-20}"  # base backoff seconds; wait = base × attempt
CYCLE_TIMEOUT="${AGENT_CYCLE_TIMEOUT:-1800}"   # GAP-3: hard wall-clock cap per cycle
WEDGE_REPEATS="${AGENT_WEDGE_REPEATS:-3}"      # GAP-3: identical cycles before 2h pause
# TRANSIENT signals — short-lived overload/rate-limit/timeout that clears on its own;
# retry with backoff. HARD signals — a real usage/quota cap; cool down for 2h.
SOFT_RE='overloaded_error|\boverloaded\b|rate limit exceeded|429 too many requests|too many requests\b|\b503\b|service unavailable|\b529\b|connection error|request timed out|\btimeout\b'
HARD_RE='usage limit reached|hit your usage limit|approaching .*usage limit|resets at|insufficient_quota|quota exceeded|out of (credits|tokens)'

# One request per cycle fetches the agent's full dashboard state; the parsers
# below pull individual fields out of the cached JSON so we don't hammer the API.
DASH_JSON=""
fetch_agent_state() {
  DASH_JSON="$(curl -s -m 8 "$DASH/agents/$AID/pause?project=$PROJ" 2>/dev/null)"
}
json_int() {   # json_int <key> -> integer value or empty
  printf '%s' "$DASH_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p" | head -1
}
json_bool() {  # json_bool <key> -> "true" | "false" | ""
  printf '%s' "$DASH_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p" | head -1
}
set_pause() {  # minutes
  curl -s -m 8 -o /dev/null -X POST "$DASH/agents/pause?project=$PROJ" \
    --data "agent_id=$AID&minutes=$1"
}

# --- Continuous liveness heartbeat --------------------------------------------
# The coordinator only refreshes last_seen when the LLM voluntarily calls an MCP
# tool (heartbeat/my_queue/claim_issue). A long model run or verify_run makes no
# such call for minutes, so the worker crosses agent_stale_seconds and gets
# falsely reclaimed while perfectly alive. This sidecar pings a lightweight
# liveness endpoint every HB_INTERVAL for the LIFETIME of the loop — through
# work cycles AND inter-cycle sleeps — so liveness is continuous and
# runtime-agnostic (codex/claude/qwen); a short stale window (~120s) is safe.
# It preserves busy/idle status server-side (only offline is revived; a stopped
# loop's trap kills the pinger, so a dead worker still goes stale). Self-exits
# if this script dies (kill -0 "$$"), so a SIGKILLed loop leaves no orphan that
# keeps a dead worker looking alive.
HB_INTERVAL="${AGENT_HEARTBEAT_SECONDS:-20}"
HB_PID=""
start_heartbeat() {
  [ "$HB_INTERVAL" -gt 0 ] 2>/dev/null || return 0
  ( while kill -0 "$$" 2>/dev/null; do
      curl -s -m 8 -o /dev/null -X POST "$DASH/agents/$AID/heartbeat?project=$PROJ" 2>/dev/null || true
      sleep "$HB_INTERVAL"
    done ) &
  HB_PID=$!
}
stop_heartbeat() {
  [ -n "$HB_PID" ] && kill "$HB_PID" 2>/dev/null || true
  HB_PID=""
}
trap 'stop_heartbeat' EXIT INT TERM

# Durable per-agent log (survives tmux scrollback + temp cleanup): tail -f "$LOG".
LOGDIR="${AGENT_LOG_DIR:-$HOME/.cache/orch-agent-logs}"
mkdir -p "$LOGDIR" 2>/dev/null || true
LOG="$LOGDIR/agent-$AID.log"

mode_label="non-interactive"; [ "$INTERACTIVE" = "1" ] && mode_label="interactive/TUI"
echo "== agent $AID dashboard-driven loop ($mode_label) :: cmd = $* (retries=$AGENT_RETRIES) ==" | tee -a "$LOG"
echo "== durable log: $LOG =="
idle=0
soft=0   # consecutive transient-limit retries in the current streak
same=0; last_hash=""   # GAP-3 wedge detector state
start_heartbeat        # loop-lifetime: beats through cycles and inter-cycle sleeps
while true; do
  # --- dashboard state (pause + loop policy), re-read every cycle so live edits
  #     on the Agents page take effect without relaunching -------------------
  fetch_agent_state
  secs="$(json_int pause_seconds)"; secs="${secs:-0}"
  loop_enabled="$(json_bool loop_enabled)"          # "true"/"false"/"" (unreachable)
  dash_poll="$(json_int poll_interval_seconds)"     # dashboard cadence, else empty
  eff_poll="${dash_poll:-$POLL}"                    # dashboard interval wins; env is fallback

  if [ "$secs" -gt 0 ] 2>/dev/null; then
    nap=$(( secs < 300 ? secs : 300 ))
    echo "== agent $AID PAUSED — ~${secs}s left; sleeping ${nap}s then re-checking (auto-resume) =="
    sleep "$nap"; continue
  fi

  echo "----- agent $AID cycle @ $(date '+%Y-%m-%d %H:%M:%S') (poll=${eff_poll}s) -----" | tee -a "$LOG"

  # === INTERACTIVE (TUI) cycle =============================================
  # A TUI needs the real terminal: no </dev/null, no |tee, no timeout wrapper.
  # Capture/classify/wedge machinery is headless-only, so it's skipped here.
  if [ "$INTERACTIVE" = "1" ]; then
    "$@"
    if [ "$loop_enabled" = "false" ]; then
      echo "== agent $AID: dashboard loop_enabled=false -> TUI session ended, stopping ==" | tee -a "$LOG"
      break
    fi
    echo "== agent $AID: TUI session ended; sleeping ${eff_poll}s then relaunching (dashboard loop on) =="
    sleep "$eff_poll"; continue
  fi

  # === NON-INTERACTIVE (streamed/headless) cycle ===========================
  tmpf="$(mktemp)"
  # GAP-3: hard per-cycle wall-clock cap — a hung/CPU-wedged model is killed
  # instead of holding its issue past the stale window (reclaim churn).
  timeout -k 30 "$CYCLE_TIMEOUT" "$@" </dev/null 2>&1 | tee -a "$tmpf" "$LOG"
  rc="${PIPESTATUS[0]:-0}"
  if [ "$rc" = "124" ] || [ "$rc" = "137" ]; then
    echo "== agent $AID: cycle exceeded ${CYCLE_TIMEOUT}s -> killed (GAP-3); backing off ==" | tee -a "$LOG"
    rm -f "$tmpf"; sleep "$eff_poll"; continue
  fi

  # GAP-3 wedge detector: N consecutive cycles with byte-identical output means
  # the model is looping (the ~1000-junk-ADR failure mode) — pause 2h.
  hash="$(cksum "$tmpf" | cut -d' ' -f1)"
  if [ "$hash" = "${last_hash:-}" ]; then
    same=$((same + 1))
    if [ "$same" -ge "$WEDGE_REPEATS" ]; then
      echo "== agent $AID: $same identical cycles -> WEDGED; pausing 2h (GAP-3) ==" | tee -a "$LOG"
      set_pause 120; same=0; last_hash=""
      rm -f "$tmpf"; continue
    fi
  else
    same=1; last_hash="$hash"
  fi

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

  # Dashboard owns on/off: loop_enabled=false -> behave one-shot (do this cycle,
  # then stop). Only an explicit "false" stops it; an unreachable dashboard
  # ("" ) keeps looping so a coordinator blip doesn't kill live workers.
  if [ "$loop_enabled" = "false" ]; then
    echo "== agent $AID: dashboard loop_enabled=false -> one cycle done, stopping ==" | tee -a "$LOG"
    break
  fi
  if [ "$IDLE_STOP" -gt 0 ] && [ "$idle" -ge "$IDLE_STOP" ]; then
    echo "== agent $AID: $idle idle cycle(s) -> stopping (on-demand) =="; break
  fi
  sleep "$eff_poll"
done
