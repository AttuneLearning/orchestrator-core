#!/usr/bin/env python3
"""worker-watchdog — conditional, one-shot hard-restart for stalled dev workers.

Run periodically (cron / systemd timer). For each registered external dev worker it
hard-restarts the worker ONLY when ALL of these hold:
  1. the worker's heartbeat has stopped  (now - agents.last_seen > STALE_SEC)
  2. there is implementation work left for its lane in the coordinator
  3. it is not deliberately paused and its loop is enabled
  4. it has NOT already been restarted for THIS stall (once-per-stall)

"Once per stall": we record the last_seen we restarted at. If the worker never
checks in again (last_seen unchanged), we do NOT restart a second time — we log +
raise a coordinator alert for a human instead (a restart that doesn't recover it
means a deeper problem; don't storm). When the worker heartbeats again the state
resets, so a future stall earns one fresh restart.

Safe by default: pass --dry-run to see decisions without killing/relaunching.
Model is configurable (WATCHDOG_MODEL env) — e.g. run the dev lanes on a hosted
model (deepseek-4-flash) instead of a local one, which is far less prone to wedge.

The monitored workers are DERIVED from the coordinator (function='dev',
runtime='external'), so this adapts to your roster. The team -> launcher mapping
assumes the standard `start-dev-worker.sh <team>` (backend/frontend); if your
roster uses other team names, adjust LAUNCH_FOR below (same hand-edit as roles.sh).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

# --- config (stamped by setup-project / install-launchers) -----------------
INSTANCE = os.environ.get("ORCH_INSTANCE", "__PROJECT_NAME__")
WORKSPACE = "__WORKSPACE_ROOT__"
ORCH_PATH = "__ORCH_PATH__"
STATE_DIR = os.path.expanduser("~/.orch-watchdog")
# Stale window: a bit beyond the daemon's AGENT_STALE_SECONDS so we only act once
# the coordinator itself has given up on the heartbeat.
STALE_SEC = int(os.environ.get("WATCHDOG_STALE_SEC", "2100"))
MODEL = os.environ.get("WATCHDOG_MODEL", "qwen-local")
DRY_RUN = "--dry-run" in sys.argv


def LAUNCH_FOR(team: str) -> list[str]:
    """Relaunch command for a dev worker of `team`. Standard dev-worker launcher."""
    return ["./start-dev-worker.sh", team, "opencode", "-m", MODEL]


sys.path.insert(0, ORCH_PATH)
os.environ.setdefault("ORCH_INSTANCE", INSTANCE)
from orchestrator.config import load_settings          # noqa: E402
from orchestrator.db import get_pool                    # noqa: E402
from orchestrator import repository as repo             # noqa: E402


def log(msg: str) -> None:
    print(f"[watchdog {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _state_path(aid: int) -> str:
    return os.path.join(STATE_DIR, f"agent-{aid}.restart")


def _read_state(aid: int) -> float | None:
    try:
        with open(_state_path(aid)) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_state(aid: int, last_seen_epoch: float) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_state_path(aid), "w") as f:
        f.write(str(last_seen_epoch))


def _clear_state(aid: int) -> None:
    try:
        os.remove(_state_path(aid))
    except OSError:
        pass


def _pgids_for(pattern: str) -> set[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    pgids: set[int] = set()
    for pid in out:
        r = subprocess.run(["ps", "-o", "pgid=", "-p", pid], capture_output=True, text=True)
        if r.stdout.strip():
            pgids.add(int(r.stdout.strip()))
    return pgids


_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _descendants(roots: list[int]) -> set[int]:
    ps = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout
    children: dict[int, list[int]] = {}
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _cpu_jiffies(pids: set[int]) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
            after = data[data.rindex(")") + 1:].split()  # robust vs spaces in comm
            total += int(after[11]) + int(after[12])       # utime + stime
        except (OSError, IndexError, ValueError):
            pass
    return total


def _cpu_busy(match_pattern: str, sample: float = 2.0, min_core_frac: float = 0.15) -> bool:
    """True if the process tree matching `match_pattern` is actively burning CPU
    right now (instantaneous sample over `sample` seconds). This is how we tell a
    worker that is heads-down in a long blocking step (typecheck / npm test / build,
    which cannot emit a heartbeat) from one that is genuinely wedged/idle — so the
    watchdog never kills a worker that is actually making progress."""
    roots = [int(x) for x in subprocess.run(
        ["pgrep", "-f", match_pattern], capture_output=True, text=True).stdout.split()]
    if not roots:
        return False
    j0 = _cpu_jiffies(_descendants(roots))
    time.sleep(sample)
    j1 = _cpu_jiffies(_descendants(roots))
    frac = ((j1 - j0) / _CLK_TCK) / sample
    return frac > min_core_frac


def hard_restart(agent: dict) -> None:
    aid = agent["id"]
    wrapper_re = rf"run-agent-loop\.sh {aid}\b"
    # 1. SIGTERM the loop-wrapper process group(s): wrapper + timeout + opencode +
    #    its children (MCP server, node) that share the group.
    for pgid in _pgids_for(wrapper_re):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(3)
    # 2. Reap any ORPHANED worker process for this agent (leaked/reparented) by its
    #    distinctive prompt text.
    subprocess.run(["pkill", "-9", "-f", f"cycle for agent {aid}"], capture_output=True)
    # 3. SIGKILL any wrapper group still standing.
    for pgid in _pgids_for(wrapper_re):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1)
    # 4. Relaunch fresh, fully detached (own session) so it survives this process.
    logf = open(f"/tmp/worker-watchdog-agent-{aid}.log", "ab")
    subprocess.Popen(
        LAUNCH_FOR(agent["team"]), cwd=WORKSPACE, stdout=logf, stderr=logf,
        stdin=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ),
    )


def _notify(pool, team: str, subject: str, body: str) -> None:
    try:
        repo.create_message(pool, from_team=team or "orchestration",
                            to_team="orch-monitor", subject=subject, body=body)
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not post coordinator alert: {exc})")


def main() -> int:
    s = load_settings(INSTANCE)
    pool = get_pool(s)
    now = time.time()
    with pool.connection() as c:
        workers = c.execute(
            "SELECT id, team, extract(epoch from last_seen), extract(epoch from paused_until), "
            "loop_enabled FROM agents WHERE function='dev' AND runtime='external' ORDER BY id"
        ).fetchall()
        for aid, team, last_seen, paused_until, loop_enabled in workers:
            agent = {"id": aid, "team": team}
            last_seen = float(last_seen) if last_seen is not None else None
            paused_until = float(paused_until) if paused_until is not None else None
            age = now - (last_seen or 0)
            paused = paused_until is not None and paused_until > now
            stale = age > STALE_SEC

            if paused or not loop_enabled:
                log(f"agent {aid} ({team}): paused/loop-off — skip")
                continue
            if not stale:
                _clear_state(aid)
                log(f"agent {aid} ({team}): alive ({int(age)}s since heartbeat) — ok")
                continue

            work = c.execute(
                "SELECT count(*) FROM issues WHERE team=%s AND gate_type='implementation' "
                "AND state IN ('in_progress','ready') "
                "AND (assigned_agent=%s OR assigned_agent IS NULL)", (team, aid)).fetchone()[0]
            if not work:
                log(f"agent {aid} ({team}): stale ({int(age)}s) but NO work waiting — skip")
                continue

            prior = _read_state(aid)
            already = prior is not None and last_seen is not None and abs(prior - last_seen) < 1
            if already:
                log(f"agent {aid} ({team}): stale + {work} work, but ALREADY restarted this "
                    f"stall (no heartbeat since) — NOT restarting again; needs human")
                if not DRY_RUN:
                    _notify(pool, team,
                            f"Watchdog: agent {aid} did not recover after one hard restart",
                            f"agent {aid} ({team}) is stale ({int(age)}s) with {work} "
                            f"implementation issues waiting; the one-shot restart did not bring "
                            f"it back. Manual intervention needed (model endpoint / memory / logs).")
                continue

            if _cpu_busy(f"cycle for agent {aid}"):
                log(f"agent {aid} ({team}): stale ({int(age)}s) but process is CPU-BUSY — "
                    f"heads-down in a long step (typecheck/test/build), NOT wedged; skip")
                continue

            log(f"agent {aid} ({team}): STALE ({int(age)}s) + {work} work + idle (not CPU-busy) "
                f"+ not-yet-restarted -> HARD RESTART{' [dry-run]' if DRY_RUN else ''} (model={MODEL})")
            if DRY_RUN:
                continue
            hard_restart(agent)
            _write_state(aid, last_seen or now)
            _notify(pool, team,
                    f"Watchdog: hard-restarted stalled agent {aid}",
                    f"agent {aid} ({team}) had no heartbeat for {int(age)}s with {work} "
                    f"implementation issues waiting; killed the leaked worker tree and relaunched "
                    f"(model={MODEL}). One-shot — will not restart again until it heartbeats.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
