"""Unit tests for the durable-worker side-car (templates/project-launchers/
agent-launchers/sidecar.py, Phase 2 of the durable-worker-sidecar plan).

sidecar.py lives under templates/, not the orchestrator package, so it is
loaded via importlib.util.spec_from_file_location. All timing is driven by a
fake monotonic clock passed into Sidecar — no test sleeps real time. A
FakeAdapter and FakeDashboard stand in for the opencode HTTP surface and the
dashboard heartbeat/pause endpoints so these tests need no network and no
model calls.
"""

from __future__ import annotations

import importlib.util
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

_SIDECAR_PATH = (
    Path(__file__).resolve().parent.parent
    / "templates" / "project-launchers" / "agent-launchers" / "sidecar.py"
)
_spec = importlib.util.spec_from_file_location("sidecar", _SIDECAR_PATH)
sidecar = importlib.util.module_from_spec(_spec)
sys.modules["sidecar"] = sidecar  # dataclass field resolution needs this registered
_spec.loader.exec_module(sidecar)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

class FakeClock:
    """Manually-advanced monotonic clock."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += delta
        return self.t


def noop_sleeper(_seconds: float) -> None:
    return None


class FakeAdapter(sidecar.Adapter):
    """In-memory adapter, ASYNCHRONOUS-capable to mirror opencode's real
    prompt_async semantics (BLOCKER 1, gate review): `inject()` returns
    immediately and does NOT mark the worker busy -- it mirrors an async
    HTTP call that returns before the worker's turn has actually started.
    Tests that need to model that gap explicitly call `begin_turn()`
    (busy=True) before `complete(text)` (busy=False, bumps the completion
    counter). Tests that don't care about the gap can just call
    `complete(text)` directly -- since `complete()` is what actually
    advances `completion_marker()`, the side-car's baseline-comparison logic
    behaves correctly either way.

    `completion_marker()` is a monotonically increasing counter, bumped only
    by `complete()` -- the freshness baseline the side-car snapshots at
    inject time and compares against before treating a bare `is_idle()` as
    "tick done"."""

    def __init__(self, *, initial_result: str | None = None, initial_completion: int = 0):
        self.injected: list[str] = []
        self.busy = False
        self._result: str | None = initial_result
        self._completion_counter = initial_completion
        self.alive_flag = True
        self.killed = False
        self.restart_count = 0
        self.clear_count = 0
        self.output_change_at: float | None = None  # t_stuck hook
        self.raise_on_inject = False
        self.raise_on_clear = False
        self.owns_process = False     # BLOCKER 3: owned-process-dead bypass
        self.proc_dead = False
        self.marker_raise_count = 0   # MAJOR 2 (opus re-review): raise this
                                       # many times on completion_marker()
                                       # before succeeding again
        self.marker_flip_remaining = 0   # QA fix (finding 7a tests): the next
                                          # N completion_marker() calls return
                                          # a distinct bogus/"unsettled" value
                                          # instead of the real one, to model
                                          # a mid-render/animated read.
        self.force_not_idle = False      # QA fix (finding 7b tests): is_idle()
                                          # always reads False regardless of
                                          # `busy`, to model whole-tail hash
                                          # instability at an otherwise-idle
                                          # prompt.

        # -- Phase 3: scripted usage + session-id tracking ---------------
        # `usage_queue` is consumed one entry per get_usage() call (FIFO);
        # once exhausted, the last value keeps being returned (mirrors a
        # real adapter reporting a steady reading), starting from None
        # (unknown) if the queue was never populated.
        self.usage_queue: list[dict | None] = []
        self._last_usage: dict | None = None
        self._session_counter = 0
        self.session_id = f"sess-{self._session_counter}"

        # -- Phase 5: scripted last_error() for the balance-alert path -----
        self._last_error: tuple[str, str] | None = None
        self.raise_on_last_error = False

    def ensure_worker(self) -> None:
        pass

    def is_idle(self) -> bool:
        if self.force_not_idle:
            return False
        return not self.busy

    def inject(self, text: str) -> None:
        if self.raise_on_inject:
            raise RuntimeError("fake inject failure")
        self.injected.append(text)
        # NOTE: busy is deliberately NOT set here -- see class docstring.

    def begin_turn(self) -> None:
        """Simulate the async worker actually starting the injected turn
        (the real opencode session/status entry appearing)."""
        self.busy = True

    def read_result(self) -> str | None:
        return self._result

    def completion_marker(self) -> str | None:
        if self.marker_raise_count > 0:
            self.marker_raise_count -= 1
            raise RuntimeError("fake completion_marker failure")
        value = str(self._completion_counter) if self._completion_counter else None
        if self.marker_flip_remaining > 0 and value is not None:
            self.marker_flip_remaining -= 1
            return f"{value}-flip"
        return value

    def clear(self) -> None:
        if self.raise_on_clear:
            raise RuntimeError("fake clear failure")
        self.clear_count += 1
        self._session_counter += 1
        self.session_id = f"sess-{self._session_counter}"

    def restart(self) -> None:
        self.restart_count += 1
        self.busy = False
        self._result = None

    def get_usage(self) -> dict | None:
        if self.usage_queue:
            self._last_usage = self.usage_queue.pop(0)
        return self._last_usage

    def current_session_id(self) -> str | None:
        return self.session_id

    def last_error(self) -> tuple[str, str] | None:
        if self.raise_on_last_error:
            raise RuntimeError("fake last_error failure")
        return self._last_error

    def set_last_error(self, name: str, message: str) -> None:
        self._last_error = (name, message)

    def alive(self) -> bool:
        return self.alive_flag

    def owned_process_dead(self) -> bool:
        return self.owns_process and self.proc_dead

    def last_output_change(self) -> float | None:
        return self.output_change_at

    def shutdown(self, kill_worker: bool) -> None:
        if kill_worker:
            self.killed = True

    # -- test helper ------------------------------------------------------
    def complete(self, tick_result_text: str) -> None:
        """Simulate the worker's turn finishing with the given assistant
        text: bumps the completion counter (the freshness marker checked
        against the side-car's tick_baseline) and goes idle."""
        self._result = tick_result_text
        self._completion_counter += 1
        self.busy = False


class FakeDashboard:
    def __init__(self, poll_interval_seconds: int = 300):
        self.heartbeat_count = 0
        self.heartbeat_fail = False
        self.last_status = None          # Phase 4: last status heartbeat() carried
        self.statuses: list[str | None] = []
        self.poll_count = 0
        self.poll_fail = False
        self.policy = {
            "pause_seconds": 0,
            "loop_enabled": True,
            "poll_interval_seconds": poll_interval_seconds,
        }
        self.wake_at = None               # Phase 4: ISO string or None
        self.last_work_at = None          # §15: orchestrator work signal, ISO or None
        self.agent_id = 1                 # Phase 5: DashboardClient shape
        self.project = "proj"

        # -- Phase 5: balance-alert (plan §6) — records of pause()/alert() --
        self.pause_calls: list[dict] = []
        self.alert_calls: list[dict] = []
        self.pause_fail = False
        self.alert_fail = False

    def heartbeat(self, status=None) -> bool:
        self.heartbeat_count += 1
        self.last_status = status
        self.statuses.append(status)
        return not self.heartbeat_fail

    def get_policy(self):
        self.poll_count += 1
        if self.poll_fail:
            return None
        payload = dict(self.policy)
        if self.wake_at is not None:
            payload["wake_at"] = self.wake_at
        if self.last_work_at is not None:
            payload["last_work_at"] = self.last_work_at
        return payload

    def pause(self, minutes=120) -> bool:
        self.pause_calls.append({"minutes": minutes})
        return not self.pause_fail

    def alert(self, subject: str, body: str = "") -> bool:
        self.alert_calls.append({"subject": subject, "body": body})
        return not self.alert_fail


class FakeTmuxRunner:
    """Stands in for TmuxAdapter's injectable `tmux_runner` seam (self._tmux)
    -- records every call (the full argv, e.g. ["tmux", "capture-pane", "-p",
    ...]) and returns a scripted `subprocess.CompletedProcess` keyed by the
    tmux subcommand (args[1]). `script()` sets the STEADY response for a
    subcommand -- every call to it returns that response until script() is
    called again for the same subcommand (so a test can change what
    `capture-pane` returns mid-test to simulate new pane output). Unscripted
    subcommands default to a bare success with empty stdout."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._responses: dict[str, tuple[int, str, str]] = {}

    def script(self, subcommand: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self._responses[subcommand] = (returncode, stdout, stderr)

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        sub = args[1] if len(args) > 1 else ""
        rc, out, err = self._responses.get(sub, (0, "", ""))
        return subprocess.CompletedProcess(args=args, returncode=rc, stdout=out, stderr=err)

    # -- test helpers -------------------------------------------------------
    def calls_for(self, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == subcommand]


def make_sidecar(adapter=None, dashboard=None, clock=None, **overrides):
    adapter = adapter or FakeAdapter()
    dashboard = dashboard or FakeDashboard()
    clock = clock or FakeClock(0.0)
    kwargs = dict(
        adapter=adapter,
        dashboard=dashboard,
        worker_prompt="WORKER PROMPT",
        tick_contract="TICK CONTRACT",
        active_window=1800,
        dormant_interval=3600,
        heartbeat_interval=20,
        state_poll_interval=45,
        t_stuck=900,
        t_max=3600,
        clock=clock,
        sleeper=noop_sleeper,
    )
    kwargs.update(overrides)
    sc = sidecar.Sidecar(**kwargs)
    return sc, adapter, dashboard, clock


# --------------------------------------------------------------------------- #
# parse_tick_result — pure function
# --------------------------------------------------------------------------- #

def test_parse_worked_ids():
    r = sidecar.parse_tick_result("blah blah\nTICK RESULT: WORKED #12 #13")
    assert r.valid and r.worked_ids == [12, 13] and not r.ready_to_clear and not r.no_work


def test_parse_worked_ready_to_clear():
    r = sidecar.parse_tick_result("did stuff\nTICK RESULT: WORKED #7; READY-TO-CLEAR")
    assert r.valid and r.worked_ids == [7] and r.ready_to_clear


def test_parse_no_work_with_reason():
    r = sidecar.parse_tick_result("nothing to do\nTICK RESULT: NO WORK (insufficient tokens)")
    assert r.valid and r.no_work and r.reason == "insufficient tokens"


def test_parse_no_work_without_reason():
    r = sidecar.parse_tick_result("TICK RESULT: NO WORK")
    assert r.valid and r.no_work and r.reason is None


def test_parse_case_insensitive_and_last_occurrence():
    text = "tick result: WORKED #1\nmore text\nTICK Result: WORKED #2 #3"
    r = sidecar.parse_tick_result(text)
    assert r.valid and r.worked_ids == [2, 3]


def test_parse_missing_marker():
    r = sidecar.parse_tick_result("I did some things but forgot the marker")
    assert not r.valid


def test_parse_none_text():
    r = sidecar.parse_tick_result(None)
    assert not r.valid


def test_build_tick_prompt_concatenation():
    text = sidecar.build_tick_prompt("WP", "CONTRACT")
    assert text == "WP\n\nCONTRACT"
    text2 = sidecar.build_tick_prompt("WP", "CONTRACT", "EXTRA")
    assert text2 == "WP\n\nCONTRACT\n\nEXTRA"


def test_resolve_t_max_default_and_floor():
    assert sidecar.resolve_t_max(None) == sidecar.DEFAULT_T_MAX_S
    assert sidecar.DEFAULT_T_MAX_S >= 3600
    assert sidecar.resolve_t_max(7200) == 7200
    with pytest.raises(ValueError):
        sidecar.resolve_t_max(3000)


# --------------------------------------------------------------------------- #
# 1. Active cadence: ticks at poll_interval; WORKED resets the 30-min window.
# --------------------------------------------------------------------------- #

def test_active_cadence_and_window_reset():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)                      # first tick fires immediately
    assert len(adapter.injected) == 1
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))
    assert sc.state == "ACTIVE"
    first_worked_at = sc.last_worked_at

    # Not due yet.
    clock.advance(299)
    sc.step(clock.t)
    assert len(adapter.injected) == 1

    # Due at poll_interval_seconds (300).
    clock.advance(1)
    sc.step(clock.t)
    assert len(adapter.injected) == 2

    adapter.complete("TICK RESULT: WORKED #2")
    sc.step(clock.advance(0.1))
    assert sc.last_worked_at > first_worked_at
    assert sc.state == "ACTIVE"


# --------------------------------------------------------------------------- #
# 2. NO-WORK ticks for > active_window -> DORMANT; dormant cadence honored;
#    WORKED flips back to ACTIVE.
# --------------------------------------------------------------------------- #

def test_dormant_transition_and_recovery():
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800, dormant_interval=3600)

    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))
    assert sc.state == "ACTIVE"

    # Keep ticking NO WORK every 300s until we cross the 1800s window.
    # NOTE: gate the completion on `sc.tick_start_at is not None` (a tick is
    # in flight), not `adapter.busy` -- the async-capable FakeAdapter no
    # longer flips `busy` on inject() (BLOCKER 1 fix), so `busy` alone can
    # no longer tell us "a fresh tick was just injected".
    for _ in range(7):
        clock.advance(300)
        sc.step(clock.t)
        if sc.tick_start_at is not None:
            adapter.complete("TICK RESULT: NO WORK")
            sc.step(clock.advance(0.1))

    assert sc.state == "DORMANT"

    # Dormant cadence is 3600s, not 300s: no tick fires 300s after going dormant.
    injected_before = len(adapter.injected)
    clock.advance(300)
    sc.step(clock.t)
    assert len(adapter.injected) == injected_before

    # ... but one fires after the full dormant interval.
    clock.advance(3600)
    sc.step(clock.t)
    assert len(adapter.injected) == injected_before + 1

    adapter.complete("TICK RESULT: WORKED #99")
    sc.step(clock.advance(0.1))
    assert sc.state == "ACTIVE"


# --------------------------------------------------------------------------- #
# 3. Coalescing: tick due x3 while worker busy -> exactly ONE inject when idle.
# --------------------------------------------------------------------------- #

def test_coalescing_absorbs_repeated_due_ticks():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)                      # tick #1 injected, stays busy
    assert len(adapter.injected) == 1

    for _ in range(3):
        clock.advance(300)                # due again while still busy
        sc.step(clock.t)
    assert len(adapter.injected) == 1      # nothing new injected while busy
    assert sc.pending is True

    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.t)                       # worker now idle -> drain exactly once
    assert len(adapter.injected) == 2
    assert sc.pending is False


# --------------------------------------------------------------------------- #
# 4. READY-TO-CLEAR -> clear() called, immediate next tick, no cadence wait.
# --------------------------------------------------------------------------- #

def test_ready_to_clear_drains_immediately():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)
    assert len(adapter.injected) == 1
    adapter.complete("TICK RESULT: WORKED #5; READY-TO-CLEAR")
    sc.step(clock.t)                       # same instant, no time advance needed
    assert adapter.clear_count == 1
    assert len(adapter.injected) == 2
    assert sc.tick_start_at == clock.t


# --------------------------------------------------------------------------- #
# 5. Watchdog t_max: busy > t_max -> restart + reinject; t_max below 3600
#    rejected at argument-validation time.
# --------------------------------------------------------------------------- #

def test_watchdog_t_max_restarts_and_reinjects():
    sc, adapter, dashboard, clock = make_sidecar(t_max=3600)

    sc.step(clock.t)                       # tick starts, worker stays busy forever
    assert len(adapter.injected) == 1

    clock.advance(3601)
    sc.step(clock.t)
    assert adapter.restart_count == 1
    assert len(adapter.injected) == 2      # restart path re-injects immediately
    assert sc.tick_start_at == clock.t


def test_t_max_below_floor_rejected():
    with pytest.raises(ValueError):
        sidecar.resolve_t_max(1000)


# --------------------------------------------------------------------------- #
# 6. loop_enabled=false suppresses ticks but heartbeats continue.
# --------------------------------------------------------------------------- #

def test_loop_disabled_suppresses_ticks_not_heartbeats():
    dashboard = FakeDashboard()
    dashboard.policy["loop_enabled"] = False
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)                       # picks up policy (state-poll forced on first step)
    assert dashboard.poll_count >= 1
    assert len(adapter.injected) == 0      # suppressed from the very first due tick

    hb_before = dashboard.heartbeat_count
    for _ in range(5):
        clock.advance(20)
        sc.step(clock.t)
    assert dashboard.heartbeat_count > hb_before
    assert len(adapter.injected) == 0


# --------------------------------------------------------------------------- #
# 7. pause_seconds > 0 suppresses ticks; resumes after.
# --------------------------------------------------------------------------- #

def test_pause_suppresses_then_resumes():
    dashboard = FakeDashboard()
    dashboard.policy["pause_seconds"] = 600
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)
    assert len(adapter.injected) == 0

    clock.advance(1)
    sc.step(clock.t)
    assert len(adapter.injected) == 0

    # Operator clears the pause; next state-poll picks it up.
    dashboard.policy["pause_seconds"] = 0
    clock.advance(45)
    sc.step(clock.t)
    assert len(adapter.injected) == 1


# --------------------------------------------------------------------------- #
# 8. Missing TICK RESULT marker x3 -> protocol-violation counter, loop
#    continues.
# --------------------------------------------------------------------------- #

def test_protocol_violations_counted_and_loop_continues():
    sc, adapter, dashboard, clock = make_sidecar()

    for _ in range(3):
        sc.step(clock.t)
        assert len(adapter.injected) >= 1
        adapter.complete("I forgot the marker entirely")
        sc.step(clock.advance(0.1))
        clock.advance(300)

    assert sc.protocol_violations == 3
    # The loop kept scheduling ticks throughout (didn't crash or wedge).
    assert len(adapter.injected) == 3

    # A valid result resets the counter.
    sc.step(clock.t)
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))
    assert sc.protocol_violations == 0


# --------------------------------------------------------------------------- #
# 9. SIGTERM handler -> clean shutdown, worker left alive.
# --------------------------------------------------------------------------- #

def test_sigterm_clean_shutdown_leaves_worker_alive():
    sc, adapter, dashboard, clock = make_sidecar(kill_worker_on_exit=False)

    assert sc._stop is False
    sc._handle_signal(signal.SIGTERM, None)
    assert sc._stop is True

    sc._shutdown()
    assert adapter.killed is False


def test_kill_worker_on_exit_flag_kills():
    sc, adapter, dashboard, clock = make_sidecar(kill_worker_on_exit=True)
    sc._handle_signal(signal.SIGINT, None)
    sc._shutdown()
    assert adapter.killed is True


# --------------------------------------------------------------------------- #
# 10. Dashboard poll failure -> cached policy used, ticking continues.
# --------------------------------------------------------------------------- #

def test_dashboard_poll_failure_uses_cached_policy():
    dashboard = FakeDashboard(poll_interval_seconds=300)
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)                       # establishes cached policy (poll_interval=300)
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))

    # Now the dashboard starts failing.
    dashboard.poll_fail = True
    clock.advance(45)
    sc.step(clock.t)                       # state-poll fails; cached policy must survive
    assert sc.policy["poll_interval_seconds"] == 300
    assert sc.policy["loop_enabled"] is True

    # Ticking still proceeds on the cached cadence.
    clock.advance(300 - 45)
    sc.step(clock.t)
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# Extra: watchdog restarts a dead worker (no tick in flight) after 3
# CONSECUTIVE alive() failures -- BLOCKER 3. A single flap must not restart;
# this was the old (buggy) synchronous-restart semantics the gate review
# flagged, so this test was updated to the new debounced behavior.
# --------------------------------------------------------------------------- #

def test_watchdog_restarts_dead_worker():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))            # tick finished, nothing in flight

    adapter.alive_flag = False
    sc.step(clock.advance(1))
    assert adapter.restart_count == 0       # 1st consecutive flap: no restart
    sc.step(clock.advance(1))
    assert adapter.restart_count == 0       # 2nd consecutive flap: still no restart
    sc.step(clock.advance(1))               # 3rd consecutive flap -> restart
    assert adapter.restart_count == 1
    assert len(adapter.injected) == 2       # restart path injects a fresh tick


# =========================================================================== #
# Phase-2 gate-review regression tests (opus review, 3 BLOCKERs + 5 MAJORs).
# =========================================================================== #

# --------------------------------------------------------------------------- #
# 10a. BLOCKER 1 (async-inject stale-read) -- THE key regression test: a
# stale completed message already sitting in the session at startup must
# never be consumed as the first tick's own result.
# --------------------------------------------------------------------------- #

def test_stale_completed_message_not_consumed_as_own_result():
    adapter = FakeAdapter(initial_completion=1, initial_result="TICK RESULT: WORKED #999")
    sc, adapter, dashboard, clock = make_sidecar(adapter=adapter)

    sc.step(clock.t)                          # injects tick #1
    assert len(adapter.injected) == 1

    # Bare idle right after inject, with the STALE message still the only
    # completed one (marker unchanged from the injection baseline) -- must
    # NOT be read as this tick's result.
    sc.step(clock.advance(0.1))
    assert sc.tick_start_at is not None         # tick still considered in flight
    assert sc.last_worked_at == 0.0             # stale #999 was NOT consumed

    # Only a FRESH completion (marker advances past the baseline) closes it.
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))
    assert sc.tick_start_at is None
    assert sc.last_worked_at == 0.2


# --------------------------------------------------------------------------- #
# 10b. False-idle window after inject (busy not yet set): no result
# collection, no double-inject.
# --------------------------------------------------------------------------- #

def test_false_idle_window_after_inject_no_collection_no_double_inject():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)                          # tick #1 injected
    assert len(adapter.injected) == 1
    assert adapter.is_idle() is True            # the async gap: reads idle already

    # A cadence-due event arrives WHILE the false-idle window is open.
    clock.advance(300)
    sc.step(clock.t)
    assert len(adapter.injected) == 1            # no double-inject
    assert sc.tick_start_at is not None

    adapter.begin_turn()                          # worker actually starts now
    clock.advance(1)
    sc.step(clock.t)
    assert len(adapter.injected) == 1

    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))
    assert len(adapter.injected) == 2              # next tick delivered normally


# --------------------------------------------------------------------------- #
# 10c. BLOCKER 2: inject() raising survives the step; pending is retried.
# --------------------------------------------------------------------------- #

def test_inject_raises_survives_and_retries():
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.raise_on_inject = True

    sc.step(clock.t)
    assert len(adapter.injected) == 0
    assert sc.pending is True
    assert sc.tick_start_at is None

    clock.advance(1)
    sc.step(clock.t)
    assert len(adapter.injected) == 0
    assert sc.pending is True

    adapter.raise_on_inject = False
    clock.advance(1)
    sc.step(clock.t)
    assert len(adapter.injected) == 1
    assert sc.pending is False


# --------------------------------------------------------------------------- #
# 10d. BLOCKER 2: clear() raising degrades to normal cadence, no crash.
# --------------------------------------------------------------------------- #

def test_clear_raises_degrades_to_normal_cadence():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)                                    # tick #1 injected
    adapter.raise_on_clear = True
    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    sc.step(clock.t)                                     # collect -> clear() raises
    assert adapter.clear_count == 0
    assert len(adapter.injected) == 1                     # no immediate re-tick
    assert sc.tick_start_at is None
    assert sc.pending is False

    clock.advance(300)                                     # normal cadence resumes
    sc.step(clock.t)
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# 10e. BLOCKER 3: a single alive() flap during an in-flight tick never
# restarts; 3 consecutive failures do.
# --------------------------------------------------------------------------- #

def test_alive_flap_during_inflight_tick_no_restart_until_third():
    sc, adapter, dashboard, clock = make_sidecar(t_max=3600)
    sc.step(clock.t)                          # tick #1 in flight, never completes
    assert len(adapter.injected) == 1

    adapter.alive_flag = False
    clock.advance(10)
    sc.step(clock.t)
    assert adapter.restart_count == 0           # 1st flap: in-flight tick preserved
    assert sc.tick_start_at is not None

    clock.advance(10)
    sc.step(clock.t)
    assert adapter.restart_count == 0           # 2nd consecutive flap: still fine

    clock.advance(10)
    sc.step(clock.t)                             # 3rd consecutive flap -> restart
    assert adapter.restart_count == 1
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# 10f. BLOCKER 3: an owned, confirmably-dead subprocess restarts immediately
# -- no debounce needed.
# --------------------------------------------------------------------------- #

def test_owned_process_dead_restarts_immediately():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    assert len(adapter.injected) == 1

    adapter.owns_process = True
    adapter.proc_dead = True
    sc.step(clock.advance(1))
    assert adapter.restart_count == 1            # immediate, first observation
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# 10g. MAJOR 5: suppression is honored on the drain path (READY-TO-CLEAR
# while paused) and the post-restart path.
# --------------------------------------------------------------------------- #

def test_suppression_honored_on_drain_path():
    dashboard = FakeDashboard()
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)                            # tick #1 injected
    dashboard.policy["pause_seconds"] = 600       # operator pauses mid-tick
    clock.advance(45)                              # state-poll picks up the pause
    sc.step(clock.t)

    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    sc.step(clock.t)                               # collect -> READY-TO-CLEAR -> drain
    assert adapter.clear_count == 1                 # clear() itself is not gated
    assert len(adapter.injected) == 1                # but the immediate re-inject was
    assert sc.pending is True

    dashboard.policy["pause_seconds"] = 0
    clock.advance(45)
    sc.step(clock.t)
    assert len(adapter.injected) == 2                 # pending delivered after unpause


def test_suppression_honored_on_post_restart_path():
    dashboard = FakeDashboard()
    dashboard.policy["pause_seconds"] = 600
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)                            # picks up paused policy; no tick injected
    assert len(adapter.injected) == 0

    adapter.alive_flag = False                   # force a dead-worker restart while paused
    for _ in range(3):
        clock.advance(10)
        sc.step(clock.t)
    assert adapter.restart_count == 1
    assert len(adapter.injected) == 0             # post-restart re-inject was suppressed too
    assert sc.pending is True

    dashboard.policy["pause_seconds"] = 0
    clock.advance(45)
    sc.step(clock.t)
    assert len(adapter.injected) == 1


# --------------------------------------------------------------------------- #
# 10h. MAJOR 6: garbage policy fields never crash and never produce an
# insane cadence.
# --------------------------------------------------------------------------- #

def test_garbage_policy_coerced_to_sane_defaults():
    dashboard = FakeDashboard()
    dashboard.policy = {"poll_interval_seconds": "not-a-number", "pause_seconds": "also-garbage"}
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)                       # forced first poll ingests the garbage
    assert sc.policy["poll_interval_seconds"] == 300
    assert sc.policy["pause_seconds"] == 0
    assert sc.policy["loop_enabled"] is True       # absent key -> safe default
    assert len(adapter.injected) == 1               # sane cadence -> not suppressed, ticks

    dashboard.policy["poll_interval_seconds"] = -50
    clock.advance(45)
    sc.step(clock.t)
    assert sc.policy["poll_interval_seconds"] == 300   # negative rejected too


# --------------------------------------------------------------------------- #
# 10i. t_stuck path driven via output_change_at (fires before t_max).
# --------------------------------------------------------------------------- #

def test_t_stuck_path_via_output_change_at():
    sc, adapter, dashboard, clock = make_sidecar(t_stuck=900, t_max=3600)
    sc.step(clock.t)                          # tick #1 injected, never completes
    assert len(adapter.injected) == 1

    adapter.output_change_at = 0.0
    clock.advance(901)
    sc.step(clock.t)
    assert adapter.restart_count == 1           # t_stuck fired before t_max
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# 10j. MAJOR 8: READY-TO-CLEAR on its own line (not the marker line) still
# triggers clear.
# --------------------------------------------------------------------------- #

def test_ready_to_clear_on_its_own_line_still_triggers_clear():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("Some handoff notes.\nREADY-TO-CLEAR\nTICK RESULT: WORKED #5")
    sc.step(clock.t)
    assert adapter.clear_count == 1
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# 10k. MINOR 9c: WORKED with no ids is treated as no work, flagged as a
# protocol violation, and does NOT reset the active window.
# --------------------------------------------------------------------------- #

def test_worked_with_no_ids_parses_as_no_work():
    r = sidecar.parse_tick_result("did some exploration\nTICK RESULT: WORKED")
    assert r.valid is True
    assert r.no_work is True
    assert r.protocol_violation is True
    assert r.worked_ids == []


def test_worked_with_no_ids_logged_as_violation_and_no_window_reset():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: WORKED")
    sc.step(clock.advance(0.1))
    assert sc.protocol_violations == 1
    assert sc.last_worked_at == 0.0        # active window NOT reset
    assert sc.state == "ACTIVE"


# =========================================================================== #
# Opus RE-review regression tests (2 empirically-reproduced error-corner
# defects + 3 minors, found after the first round of gate-review fixes).
# =========================================================================== #

# --------------------------------------------------------------------------- #
# R1. BLOCKER: restart storm while suppressed. `_restart()` used to leave
# tick_start_at at its stale pre-restart value; if the trailing post-restart
# _inject_tick then hit the suppression branch (pause / loop_enabled=false),
# tick_start_at never got re-armed, so the t_max watchdog check re-fired on
# EVERY subsequent step -> ~1 restart/step for the whole pause. Fix: _restart
# clears tick_start_at/tick_baseline unconditionally before the trailing
# inject, since the in-flight tick is abandoned by definition on restart.
# --------------------------------------------------------------------------- #

def test_restart_storm_prevented_while_suppressed():
    dashboard = FakeDashboard()
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard, t_max=3600)

    sc.step(clock.t)                              # tick #1 in flight, never completes
    assert len(adapter.injected) == 1

    dashboard.policy["pause_seconds"] = 600         # operator pauses mid-tick
    clock.advance(45)                                # state-poll picks it up
    sc.step(clock.t)

    clock.advance(3601)                               # blow past t_max while paused
    sc.step(clock.t)                                   # restart -> post-restart inject suppressed
    assert adapter.restart_count == 1
    assert len(adapter.injected) == 1                    # inject was suppressed, not delivered
    assert sc.pending is True
    assert sc.tick_start_at is None                       # BLOCKER fix: cleared, not stale

    # Several more steps, still paused and still "past" the old t_max window
    # -- must NOT restart again. (The old bug: tick_start_at stayed at its
    # stale value, so elapsed > t_max kept re-triggering a restart every
    # single step.)
    for _ in range(5):
        clock.advance(10)
        sc.step(clock.t)
    assert adapter.restart_count == 1                     # still exactly one restart

    dashboard.policy["pause_seconds"] = 0
    clock.advance(45)
    sc.step(clock.t)
    assert len(adapter.injected) == 2                       # pending delivered after unpause


# --------------------------------------------------------------------------- #
# R2. MAJOR: baseline sentinel overload reopens the stale-read bug. If the
# completion-marker snapshot at inject time raises, storing `None` for the
# baseline is indistinguishable from a LEGITIMATE `None` baseline (adapter
# read fine, nothing completed yet) -- a later successful-but-late read of
# the SAME pre-existing stale message then looks "different from None" and
# gets wrongly collected. Fix: `_BASELINE_UNKNOWN` sentinel; while unknown,
# retry the snapshot each step (while still idle) and PROMOTE the first
# successful read to the baseline instead of trusting it as a completion.
# --------------------------------------------------------------------------- #

def test_unknown_baseline_promoted_then_real_completion_collected():
    adapter = FakeAdapter(initial_completion=1, initial_result="TICK RESULT: WORKED #999")
    adapter.marker_raise_count = 1                  # raises exactly once, at inject
    sc, adapter, dashboard, clock = make_sidecar(adapter=adapter)

    sc.step(clock.t)                                  # inject -> snapshot raises -> UNKNOWN
    assert len(adapter.injected) == 1
    assert sc.tick_baseline is sidecar._BASELINE_UNKNOWN

    sc.step(clock.advance(0.1))                        # retry succeeds -> promotes to "1"
    assert sc.tick_baseline == "1"
    assert sc.tick_start_at is not None                  # still in flight, nothing collected
    assert sc.last_worked_at == 0.0                       # stale #999 was NOT consumed

    adapter.complete("TICK RESULT: WORKED #1")            # the REAL completion
    sc.step(clock.advance(0.1))
    assert sc.tick_start_at is None
    assert sc.last_worked_at == 0.2


# --------------------------------------------------------------------------- #
# R3. MINOR: MIN_T_MAX_S must equal the derived floor (verify ceiling +
# margin), not a stale literal that predates a margin bump.
# --------------------------------------------------------------------------- #

def test_min_t_max_matches_derived_floor():
    assert sidecar.MIN_T_MAX_S == sidecar.VERIFY_CEILING_S + sidecar.T_MAX_MARGIN_S
    assert sidecar.DEFAULT_T_MAX_S == sidecar.MIN_T_MAX_S
    with pytest.raises(ValueError):
        sidecar.resolve_t_max(sidecar.MIN_T_MAX_S - 1)
    assert sidecar.resolve_t_max(sidecar.MIN_T_MAX_S) == sidecar.MIN_T_MAX_S


# --------------------------------------------------------------------------- #
# R4. MINOR: _coerce_policy accepts integral floats for pause_seconds and
# poll_interval_seconds -- a float 600.0 pause must not silently un-pause.
# --------------------------------------------------------------------------- #

def test_integral_float_policy_values_accepted():
    dashboard = FakeDashboard()
    dashboard.policy = {"poll_interval_seconds": 120.0, "pause_seconds": 600.0, "loop_enabled": True}
    sc, adapter, dashboard, clock = make_sidecar(dashboard=dashboard)

    sc.step(clock.t)
    assert sc.policy["poll_interval_seconds"] == 120
    assert isinstance(sc.policy["poll_interval_seconds"], int)
    assert sc.policy["pause_seconds"] == 600            # NOT silently un-paused
    assert isinstance(sc.policy["pause_seconds"], int)
    assert len(adapter.injected) == 0                     # still paused -> suppressed


def test_non_integral_float_policy_falls_back_to_default():
    r = sidecar._coerce_policy({"pause_seconds": 600.5, "poll_interval_seconds": 120.5,
                                 "loop_enabled": True}, dict(sidecar.DEFAULT_POLICY))
    assert r["pause_seconds"] == sidecar.DEFAULT_POLICY["pause_seconds"]
    assert r["poll_interval_seconds"] == sidecar.DEFAULT_POLICY["poll_interval_seconds"]


# --------------------------------------------------------------------------- #
# R5. MINOR: READY-TO-CLEAR must be anchored to the literal uppercase token
# -- lowercase prose that happens to contain the phrase must not trigger a
# context clear, but the uppercase token still works anywhere (own line or
# the marker line, tolerant of hyphen/space/none between words).
# --------------------------------------------------------------------------- #

def test_lowercase_ready_to_clear_prose_does_not_trigger():
    r = sidecar.parse_tick_result(
        "we are near the ready-to-clear point but not quite there yet\n"
        "TICK RESULT: WORKED #5"
    )
    assert r.valid is True
    assert r.worked_ids == [5]
    assert r.ready_to_clear is False


def test_uppercase_ready_to_clear_token_variants_still_trigger():
    assert sidecar.parse_tick_result("TICK RESULT: WORKED #1; READY-TO-CLEAR").ready_to_clear is True
    assert sidecar.parse_tick_result("TICK RESULT: WORKED #1; READY TO CLEAR").ready_to_clear is True
    assert sidecar.parse_tick_result(
        "notes\nREADY-TO-CLEAR\nTICK RESULT: WORKED #1"
    ).ready_to_clear is True


# =========================================================================== #
# Phase 3: token accounting + budget-embedded ticks (plan §6, phase3-token
# spec). FakeAdapter gained get_usage() (scripted via `usage_queue`) and
# current_session_id() (bumped by clear()) for these tests.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# P3-1. budget_line appears in every injected tick prompt.
# --------------------------------------------------------------------------- #

def test_budget_line_present_in_every_tick_prompt():
    sc, adapter, dashboard, clock = make_sidecar()

    sc.step(clock.t)                                   # tick #1: no usage known yet
    assert "CONTEXT BUDGET" in adapter.injected[0]
    assert "CONTEXT BUDGET: unknown" in adapter.injected[0]

    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))
    clock.advance(300)
    sc.step(clock.t)                                    # tick #2
    assert "CONTEXT BUDGET" in adapter.injected[-1]


# --------------------------------------------------------------------------- #
# P3-2. Usage below clear threshold -> no "nearly full" appendix; usage at/
# above threshold -> appendix present.
# --------------------------------------------------------------------------- #

def test_budget_appendix_appears_only_above_clear_threshold():
    adapter = FakeAdapter()
    sc, adapter, dashboard, clock = make_sidecar(
        adapter=adapter, context_limit_tokens=1000, context_clear_pct=70, context_low_pct=90)

    sc.step(clock.t)                                    # tick #1: unknown budget
    adapter.usage_queue = [{"context_tokens": 500, "session_cost": 0.1}]   # 50% < clear_pct
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))                          # collect -> accountant now at 50%

    clock.advance(300)
    sc.step(clock.t)                                      # tick #2 reflects 50%
    assert "CONTEXT BUDGET" in adapter.injected[-1]
    assert "CONTEXT NEARLY FULL" not in adapter.injected[-1]

    adapter.usage_queue = [{"context_tokens": 750, "session_cost": 0.2}]    # 75% >= clear_pct
    adapter.complete("TICK RESULT: WORKED #2")
    sc.step(clock.advance(0.1))                            # collect -> accountant now at 75%

    clock.advance(300)
    sc.step(clock.t)                                        # tick #3 reflects 75% -> appendix
    assert "CONTEXT NEARLY FULL" in adapter.injected[-1]


# --------------------------------------------------------------------------- #
# P3-3. exhausted() + idle + no tick in flight -> FORCED_CLEAR fires (backstop
# for a worker that never says READY-TO-CLEAR); context resets to unknown,
# cumulative cost survives the clear, and the session id visibly rotates.
# --------------------------------------------------------------------------- #

def test_forced_clear_when_exhausted_idle_no_tick_in_flight(capsys):
    adapter = FakeAdapter()
    sc, adapter, dashboard, clock = make_sidecar(
        adapter=adapter, context_limit_tokens=1000, context_clear_pct=70, context_low_pct=90)

    sc.step(clock.t)                                       # tick #1 injected
    old_session = adapter.session_id
    adapter.usage_queue = [{"context_tokens": 950, "session_cost": 1.5}]   # 95% >= low_pct
    adapter.complete("TICK RESULT: WORKED #1")               # NOTE: no READY-TO-CLEAR
    sc.step(clock.advance(0.1))                               # collect -> exhausted -> forced clear same step

    assert adapter.clear_count == 1
    assert adapter.session_id != old_session
    assert sc.accountant.context_tokens is None                # per-session context reset
    assert sc.accountant.total_cost == pytest.approx(1.5)       # cumulative cost preserved

    captured = capsys.readouterr()
    assert "event=FORCED_CLEAR" in captured.out
    assert "reason=context" in captured.out
    assert f"session_id={adapter.session_id}" in captured.out


def test_forced_clear_never_fires_mid_tick():
    adapter = FakeAdapter()
    sc, adapter, dashboard, clock = make_sidecar(
        adapter=adapter, context_limit_tokens=1000, context_clear_pct=70, context_low_pct=90)

    sc.step(clock.t)                                        # tick #1 in flight, never completes
    adapter.usage_queue = [{"context_tokens": 950, "session_cost": 0.5}]
    # Feed usage without ever collecting a result (worker stays busy) --
    # exhaustion alone, mid-tick, must never trigger a clear.
    sc.accountant.update(adapter.get_usage())
    assert sc.accountant.exhausted() is True

    clock.advance(1)
    sc.step(clock.t)
    assert adapter.clear_count == 0                          # tick_start_at is not None -> no forced clear


# --------------------------------------------------------------------------- #
# P3-4. get_usage() returning None (adapter has nothing to report) never
# crashes the side-car and keeps the conservative unknown-budget line.
# --------------------------------------------------------------------------- #

def test_get_usage_none_never_crashes_and_uses_unknown_line():
    adapter = FakeAdapter()                                  # usage_queue empty -> get_usage() -> None
    sc, adapter, dashboard, clock = make_sidecar(adapter=adapter)

    sc.step(clock.t)
    assert "CONTEXT BUDGET: unknown" in adapter.injected[0]

    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.advance(0.1))                               # collect with usage still None
    assert sc.accountant.context_tokens is None
    assert sc.accountant.exhausted() is False
    assert sc.accountant.should_request_clear() is False

    clock.advance(300)
    sc.step(clock.t)                                           # loop continues normally
    assert len(adapter.injected) == 2


# --------------------------------------------------------------------------- #
# P3-5. READY-TO-CLEAR drain still works with the budget layer wired in: the
# clear resets context, and the IMMEDIATE re-inject reflects the fresh
# (unknown, not stale-high) session usage.
# --------------------------------------------------------------------------- #

def test_ready_to_clear_drain_with_budget_layer_reflects_fresh_usage():
    adapter = FakeAdapter()
    sc, adapter, dashboard, clock = make_sidecar(
        adapter=adapter, context_limit_tokens=1000, context_clear_pct=70, context_low_pct=90)

    sc.step(clock.t)                                           # tick #1
    old_session = adapter.session_id
    adapter.usage_queue = [{"context_tokens": 800, "session_cost": 0.5}]    # 80% -> request-clear
    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    sc.step(clock.t)                                             # collect -> drain clear -> immediate re-inject

    assert adapter.clear_count == 1
    assert adapter.session_id != old_session
    assert len(adapter.injected) == 2
    assert "CONTEXT BUDGET: unknown" in adapter.injected[-1]      # fresh session, not stale 80%
    assert "CONTEXT NEARLY FULL" not in adapter.injected[-1]


# --------------------------------------------------------------------------- #
# P3-6. Cost accumulates correctly across two sessions (pre/post clear): the
# running total is the sum, never reset to the new session's figure alone.
# --------------------------------------------------------------------------- #

def test_cost_accumulates_across_two_sessions_through_clear():
    adapter = FakeAdapter()
    sc, adapter, dashboard, clock = make_sidecar(adapter=adapter, context_limit_tokens=1000)

    sc.step(clock.t)                                            # tick #1
    adapter.usage_queue = [{"context_tokens": 200, "session_cost": 1.25}]
    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    sc.step(clock.t)                                              # collect -> drain clear -> folds 1.25 into baseline
    assert sc.accountant.total_cost == pytest.approx(1.25)
    assert len(adapter.injected) == 2                              # drain's immediate re-inject (tick #2)

    adapter.usage_queue = [{"context_tokens": 300, "session_cost": 0.75}]
    adapter.complete("TICK RESULT: WORKED #2")
    sc.step(clock.advance(0.1))                                    # collect tick #2's usage

    assert sc.accountant.total_cost == pytest.approx(2.0)           # 1.25 baseline + 0.75 current session


# --------------------------------------------------------------------------- #
# P3-7. TICK_INJECT/CLEAR log lines carry session_id for operator log
# correlation across a clear.
# --------------------------------------------------------------------------- #

def test_tick_inject_log_includes_session_id(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    captured = capsys.readouterr()
    assert f"session_id={adapter.session_id}" in captured.out


def test_clear_log_includes_session_id_after_drain(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    capsys.readouterr()                                          # drop tick #1's noise
    sc.step(clock.t)
    captured = capsys.readouterr()
    assert "event=CLEAR " in captured.out or captured.out.rstrip().endswith("event=CLEAR")
    assert f"session_id={adapter.session_id}" in captured.out


# --------------------------------------------------------------------------- #
# P3-8. CLI: --context-limit-tokens/--context-clear-pct/--context-low-pct/
# --budget-margin-pct defaults, and validation of 0 < clear < low <= 100.
# --------------------------------------------------------------------------- #

def test_cli_context_flag_defaults():
    parser = sidecar.build_arg_parser()
    args = parser.parse_args([
        "--agent-id", "1", "--project", "p", "--dashboard", "http://x",
        "--opencode-url", "http://y", "--prompt-file", "/nonexistent",
    ])
    assert args.context_limit_tokens == 180_000
    assert args.context_clear_pct == 70
    assert args.context_low_pct == 90
    assert args.budget_margin_pct == 15


def test_cli_rejects_clear_pct_not_below_low_pct(capsys):
    with pytest.raises(SystemExit):
        sidecar.main([
            "--agent-id", "1", "--project", "p", "--dashboard", "http://x",
            "--runtime", "opencode", "--opencode-url", "http://y",
            "--prompt-file", "/nonexistent",
            "--context-clear-pct", "90", "--context-low-pct", "70",
        ])
    assert "context-clear-pct" in capsys.readouterr().err


def test_cli_rejects_low_pct_over_100():
    with pytest.raises(SystemExit):
        sidecar.main([
            "--agent-id", "1", "--project", "p", "--dashboard", "http://x",
            "--runtime", "opencode", "--opencode-url", "http://y",
            "--prompt-file", "/nonexistent",
            "--context-clear-pct", "70", "--context-low-pct", "150",
        ])


# --------------------------------------------------------------------------- #
# P3-9. TokenAccountant unit-level checks (no Sidecar involved).
# --------------------------------------------------------------------------- #

def test_token_accountant_update_tolerates_malformed_usage():
    acc = sidecar.TokenAccountant(context_limit_tokens=1000)
    acc.update(None)
    assert acc.context_tokens is None
    acc.update({"context_tokens": "garbage", "session_cost": "also-garbage"})
    assert acc.context_tokens is None
    assert acc.total_cost == 0.0
    acc.update({"context_tokens": 100, "session_cost": 0.5})
    assert acc.context_tokens == 100
    assert acc.total_cost == pytest.approx(0.5)
    # A subsequent malformed reading must not wipe out the last-good state.
    acc.update({"context_tokens": None, "session_cost": None})
    assert acc.context_tokens == 100
    assert acc.total_cost == pytest.approx(0.5)


def test_token_accountant_pct_thresholds():
    acc = sidecar.TokenAccountant(context_limit_tokens=1000, clear_threshold_pct=70, low_budget_pct=90)
    assert acc.should_request_clear() is False
    assert acc.exhausted() is False

    acc.update({"context_tokens": 699, "session_cost": 0.0})
    assert acc.should_request_clear() is False
    acc.update({"context_tokens": 700, "session_cost": 0.0})
    assert acc.should_request_clear() is True
    assert acc.exhausted() is False

    acc.update({"context_tokens": 900, "session_cost": 0.0})
    assert acc.exhausted() is True


def test_token_accountant_reset_session_folds_cost_and_clears_context():
    acc = sidecar.TokenAccountant(context_limit_tokens=1000)
    acc.update({"context_tokens": 400, "session_cost": 2.0})
    acc.reset_session()
    assert acc.context_tokens is None
    assert acc.total_cost == pytest.approx(2.0)

    acc.update({"context_tokens": 50, "session_cost": 0.3})
    assert acc.total_cost == pytest.approx(2.3)                 # baseline 2.0 + new session 0.3


# =========================================================================== #
# QA review fixes (2026-07-23): 2 MAJOR + 1 MEDIUM accounting-math defects
# found after the Phase-3 implementation landed.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# QA-1. MAJOR: OpencodeAdapter.get_usage() must count cache.write and
# reasoning tokens too, not just input + cache.read + output -- and must
# prefer the server's own `tokens.total` when it's a sane positive int.
# --------------------------------------------------------------------------- #

def _oc_usage_stub(turn_tokens, cost):
    """Stub OpencodeAdapter._request for get_usage: the LAST completed
    assistant message carries `turn_tokens`; GET /session carries cumulative
    cost. Mirrors the two calls get_usage makes since DEFECT-SIDECAR-2 (per-turn
    tokens from /message, cumulative cost from /session)."""
    messages = [{"info": {"role": "assistant", "time": {"completed": 1},
                          "tokens": turn_tokens}}]

    def _request(method, path, payload=None, timeout=None):
        if path.endswith("/message"):
            return messages
        return {"cost": cost}
    return _request


def test_opencode_adapter_get_usage_prefers_total_when_present():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    adapter._request = _oc_usage_stub(
        {"input": 100, "output": 50, "reasoning": 20,
         "cache": {"read": 10, "write": 5}, "total": 999}, cost=1.23)
    assert adapter.get_usage() == {"context_tokens": 999, "session_cost": 1.23}


def test_opencode_adapter_get_usage_sums_all_fields_including_cache_write_and_reasoning():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    # No per-turn `total` -> sum ALL parts, including cache.write (15) and
    # reasoning (20) -- the two fields the pre-QA-fix code silently dropped.
    adapter._request = _oc_usage_stub(
        {"input": 100, "output": 50, "reasoning": 20,
         "cache": {"read": 10, "write": 15}}, cost=0.5)
    assert adapter.get_usage() == {"context_tokens": 195, "session_cost": 0.5}   # 100+50+20+10+15


def test_opencode_adapter_get_usage_falls_back_to_sum_when_total_missing_or_zero():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    adapter._request = _oc_usage_stub(
        {"input": 10, "output": 5, "reasoning": 0,
         "cache": {"read": 0, "write": 0}, "total": 0}, cost=0.0)
    assert adapter.get_usage() == {"context_tokens": 15, "session_cost": 0.0}


def test_opencode_adapter_get_usage_tracks_last_turn_not_session_cumulative():
    """DEFECT-SIDECAR-2 regression (plan §14): context occupancy must come from
    the LAST completed assistant turn, NOT the session's cumulative token total.
    In the first soak (2026-07-23) reading the session sum reported
    context_tokens=2,860,186 (1589% of a 180k window) after one work cycle and
    forced a spurious clear. Here the session object still carries that
    cumulative trap, but the reported context must equal only the final turn."""
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    messages = [
        {"info": {"role": "assistant", "time": {"completed": 1},
                  "tokens": {"input": 50000, "output": 40000, "reasoning": 0,
                             "cache": {"read": 0, "write": 0}}}},
        {"info": {"role": "user", "time": {"completed": 2}}},
        {"info": {"role": "assistant", "time": {"completed": 3},
                  "tokens": {"input": 18000, "output": 1000, "reasoning": 0,
                             "cache": {"read": 24, "write": 27}}}},
    ]

    def _request(method, path, payload=None, timeout=None):
        if path.endswith("/message"):
            return messages
        return {"tokens": {"total": 2_860_186}, "cost": 3.5}   # cumulative session trap

    adapter._request = _request
    usage = adapter.get_usage()
    assert usage["context_tokens"] == 19051   # last turn only: 18000+1000+0+24+27
    assert usage["session_cost"] == 3.5


def test_opencode_adapter_get_usage_returns_none_on_request_failure():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"

    def _raise(*a, **kw):
        raise RuntimeError("boom")
    adapter._request = _raise
    assert adapter.get_usage() is None


def test_opencode_adapter_get_usage_none_when_no_completed_turn_yet():
    """Right after clear() the fresh session has no completed assistant turn --
    get_usage must degrade to unknown (None), not report 0 (which would read as
    an empty context and defeat the budget layer)."""
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    adapter._request = lambda method, path, payload=None, timeout=None: (
        [] if path.endswith("/message") else {"cost": 0.0})
    assert adapter.get_usage() is None


# --------------------------------------------------------------------------- #
# QA-2. MAJOR: TokenAccountant.update() must never let a stale/smaller cost
# reading regress `_session_cost` -- it must take the max, so a later
# reset_session() folds the true high-water mark into the baseline, not a
# transient dip.
# --------------------------------------------------------------------------- #

def test_token_accountant_cost_never_regresses_on_stale_reading():
    acc = sidecar.TokenAccountant(context_limit_tokens=1000)
    acc.update({"context_tokens": 100, "session_cost": 0.5})
    acc.update({"context_tokens": 100, "session_cost": 0.3})    # stale/smaller reading
    assert acc.total_cost == pytest.approx(0.5)                  # must NOT have regressed
    acc.reset_session()
    assert acc.total_cost == pytest.approx(0.5)                  # folded high-water mark, not 0.3


# --------------------------------------------------------------------------- #
# QA-3. MEDIUM: --context-limit-tokens <= 0 must be rejected at parse time --
# otherwise context_pct() returns None/negative forever and the whole safety
# layer (should_request_clear/exhausted) is silently disabled.
# --------------------------------------------------------------------------- #

def test_cli_rejects_zero_context_limit_tokens(capsys):
    with pytest.raises(SystemExit):
        sidecar.main([
            "--agent-id", "1", "--project", "p", "--dashboard", "http://x",
            "--runtime", "opencode", "--opencode-url", "http://y",
            "--prompt-file", "/nonexistent",
            "--context-limit-tokens", "0",
        ])
    assert "context-limit-tokens" in capsys.readouterr().err


def test_cli_rejects_negative_context_limit_tokens(capsys):
    with pytest.raises(SystemExit):
        sidecar.main([
            "--agent-id", "1", "--project", "p", "--dashboard", "http://x",
            "--runtime", "opencode", "--opencode-url", "http://y",
            "--prompt-file", "/nonexistent",
            "--context-limit-tokens", "-100",
        ])
    assert "context-limit-tokens" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Phase 4 (plan §7, migration 0024): wake relay -- check_wake dedup rules.
# --------------------------------------------------------------------------- #

def test_wake_first_observation_establishes_baseline_without_firing(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))
    injected_before = len(adapter.injected)
    pending_before = sc.pending
    capsys.readouterr()

    sc.check_wake({"wake_at": "2026-01-01T00:00:00+00:00"})

    assert sc._last_wake_at is not None
    assert sc.pending == pending_before
    assert len(adapter.injected) == injected_before
    assert "event=WAKE" not in capsys.readouterr().out


def test_wake_increase_fires_exactly_once(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))

    sc.check_wake({"wake_at": "2026-01-01T00:00:00+00:00"})   # baseline, no fire
    capsys.readouterr()

    sc.check_wake({"wake_at": "2026-01-01T00:05:00+00:00"})   # strictly greater -> fires
    assert "event=WAKE" in capsys.readouterr().out
    assert sc.state == "ACTIVE"
    assert sc.pending is True

    injected_before = len(adapter.injected)
    sc.step(clock.t)                       # normal machinery actually delivers it
    assert len(adapter.injected) == injected_before + 1

    # A second read of the SAME wake_at must never re-fire.
    capsys.readouterr()
    sc.check_wake({"wake_at": "2026-01-01T00:05:00+00:00"})
    assert "event=WAKE" not in capsys.readouterr().out


def test_wake_equal_value_does_not_fire():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.check_wake({"wake_at": "2026-01-01T00:00:00+00:00"})   # baseline
    sc.pending = False
    sc.check_wake({"wake_at": "2026-01-01T00:00:00+00:00"})   # equal, not greater
    assert sc.pending is False


def test_wake_during_dormant_goes_active_with_immediate_tick():
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800, dormant_interval=3600)
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))
    for _ in range(7):
        clock.advance(300)
        sc.step(clock.t)
        if sc.tick_start_at is not None:
            adapter.complete("TICK RESULT: NO WORK")
            sc.step(clock.advance(0.1))
    assert sc.state == "DORMANT"

    sc.check_wake({"wake_at": "2026-01-01T00:00:00+00:00"})   # baseline only, no fire
    assert sc.state == "DORMANT"

    injected_before = len(adapter.injected)
    sc.check_wake({"wake_at": "2026-01-01T00:05:00+00:00"})   # increase
    assert sc.state == "ACTIVE"
    assert sc.pending is True

    sc.step(clock.t)                       # delivered through the normal path
    assert len(adapter.injected) == injected_before + 1


def test_state_poll_relays_dashboard_wake_at_end_to_end():
    """_maybe_poll_state itself feeds the fetched payload into check_wake --
    not just check_wake called directly."""
    sc, adapter, dashboard, clock = make_sidecar(state_poll_interval=45)
    sc.step(clock.t)                       # forced first poll; dashboard.wake_at is
                                            # still None here, nothing to relay yet
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))

    dashboard.wake_at = "2026-01-01T00:00:00+00:00"
    clock.advance(50)                      # comfortably past state_poll_interval (45)
    sc.step(clock.t)                       # first non-null observation: baseline only
    assert sc._last_wake_at is not None
    assert sc.state == "ACTIVE"            # unchanged, no fire yet

    dashboard.wake_at = "2026-01-01T00:05:00+00:00"
    clock.advance(50)
    injected_before = len(adapter.injected)
    sc.step(clock.t)
    assert len(adapter.injected) == injected_before + 1
    assert sc.state == "ACTIVE"


# --------------------------------------------------------------------------- #
# Phase 4: heartbeats now carry a working|idle|dormant status self-report.
# --------------------------------------------------------------------------- #

def test_heartbeat_carries_working_status_while_tick_in_flight():
    sc, adapter, dashboard, clock = make_sidecar(heartbeat_interval=20)
    sc.step(clock.t)                       # tick #1 injected, worker stays busy forever
    assert sc.tick_start_at is not None
    clock.advance(20)
    sc.step(clock.t)                       # heartbeat fires again, tick now in flight
    assert dashboard.last_status == "working"


def test_heartbeat_carries_idle_status_when_active_with_no_tick_in_flight():
    sc, adapter, dashboard, clock = make_sidecar(heartbeat_interval=20)
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))
    assert sc.tick_start_at is None
    assert sc.state == "ACTIVE"
    clock.advance(20)
    sc.step(clock.t)
    assert dashboard.last_status == "idle"


def test_heartbeat_carries_dormant_status_when_dormant():
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800, dormant_interval=3600,
                                                 heartbeat_interval=20)
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))
    for _ in range(7):
        clock.advance(300)
        sc.step(clock.t)
        if sc.tick_start_at is not None:
            adapter.complete("TICK RESULT: NO WORK")
            sc.step(clock.advance(0.1))
    assert sc.state == "DORMANT"
    clock.advance(20)
    sc.step(clock.t)
    assert dashboard.last_status == "dormant"


# --------------------------------------------------------------------------- #
# Phase 4: dashboard-supplied cadence-window overrides (active_window_seconds /
# dormant_interval_seconds) prefer the policy payload over the CLI defaults,
# within the same bounds repository.set_agent_loop enforces server-side.
# --------------------------------------------------------------------------- #

def test_policy_cadence_window_overrides_cli_defaults():
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800, dormant_interval=3600,
                                                 state_poll_interval=45)
    dashboard.policy["active_window_seconds"] = 600
    dashboard.policy["dormant_interval_seconds"] = 7200

    sc.step(clock.t)                       # forced first-step state poll

    assert sc.active_window == 600
    assert sc.dormant_interval == 7200


def test_policy_cadence_window_out_of_bounds_keeps_cli_value():
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800, dormant_interval=3600,
                                                 state_poll_interval=45)
    dashboard.policy["active_window_seconds"] = 100          # below MIN_ACTIVE_WINDOW_S (300)
    dashboard.policy["dormant_interval_seconds"] = 999_999   # above MAX_DORMANT_INTERVAL_S (86400)

    sc.step(clock.t)

    assert sc.active_window == 1800
    assert sc.dormant_interval == 3600


# --------------------------------------------------------------------------- #
# Phase 5: TmuxAdapter (plan §5 + §12) — driven entirely through
# FakeTmuxRunner, never a real tmux binary.
# --------------------------------------------------------------------------- #

def make_tmux_adapter(runner=None, clock=None, **overrides):
    runner = runner or FakeTmuxRunner()
    clock = clock or FakeClock(0.0)
    kwargs = dict(tmux_target="agents:1.0", spawn_cmd="claude --dangerously-skip-permissions",
                  project="proj", agent_id=7, idle_quiet_seconds=10,
                  tmux_runner=runner, clock=clock)
    kwargs.update(overrides)
    adapter = sidecar.TmuxAdapter(**kwargs)
    return adapter, runner, clock


def test_tmux_alive_true_when_pane_running_tui():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("display-message", stdout="claude\n")
    assert adapter.alive() is True


def test_tmux_alive_false_when_pane_is_bare_shell():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("display-message", stdout="bash\n")
    assert adapter.alive() is False


def test_tmux_alive_false_when_display_message_fails():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("display-message", returncode=1, stderr="can't find pane")
    assert adapter.alive() is False


# --------------------------------------------------------------------------- #
# BUGFIX (coordinator live-tmux e2e report): a bash-driven fake-TUI/wrapper
# reports pane_current_command == "bash" for its ENTIRE healthy lifetime --
# the bare-shell-means-dead heuristic must not fire when that IS what we
# expect to see (spawn_cmd itself launches a shell), only when we expect a
# real TUI and see a shell instead (the unambiguous "it died" case).
# --------------------------------------------------------------------------- #

def test_tmux_alive_true_for_shell_driven_spawn_cmd_even_when_pane_shows_shell():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="bash /path/to/fake-tui.sh")
    runner.script("display-message", stdout="bash\n")
    assert adapter.alive() is True


def test_tmux_alive_true_for_shell_driven_spawn_cmd_regardless_of_shell_flavor():
    # expected="zsh" (itself a shell) -- observed "bash" differs from
    # expected but is STILL a shell, so still counts as alive under the same
    # "can't tell shell-running-script from shell-after-exit" reasoning.
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="zsh -c 'echo hi'")
    runner.script("display-message", stdout="bash\n")
    assert adapter.alive() is True


def test_tmux_alive_true_when_observed_command_matches_expected_exactly():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="claude --dangerously-skip-permissions")
    runner.script("display-message", stdout="claude\n")
    assert adapter.alive() is True


def test_tmux_alive_false_when_real_tui_expected_but_shell_observed():
    # Unchanged: a genuine non-shell TUI (claude/codex) that dropped back to
    # a bare shell is still correctly detected as dead.
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="claude --dangerously-skip-permissions")
    runner.script("display-message", stdout="bash\n")
    assert adapter.alive() is False


def test_tmux_alive_false_without_spawn_cmd_bare_shell_unchanged():
    # No spawn_cmd configured (nothing to compare against) -- falls back to
    # the plain heuristic, unchanged from before this fix.
    adapter, runner, clock = make_tmux_adapter(spawn_cmd=None)
    runner.script("display-message", stdout="bash\n")
    assert adapter.alive() is False


def test_tmux_ensure_worker_noop_for_shell_driven_spawn_cmd_already_running():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="bash fake-tui.sh")
    runner.script("display-message", stdout="bash\n")
    adapter.ensure_worker()
    assert not runner.calls_for("respawn-pane")


def test_tmux_ensure_worker_noop_when_alive():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("display-message", stdout="claude\n")
    adapter.ensure_worker()
    assert not runner.calls_for("respawn-pane")


def test_tmux_ensure_worker_respawns_when_dead_with_spawn_cmd():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="claude --foo")
    runner.script("display-message", stdout="bash\n")     # dead TUI, bare shell left behind
    adapter.ensure_worker()
    # finding 6: respawn ALWAYS goes through an explicit bash -lc, never the
    # pane's own default shell, since spawn_cmd may itself be a composite
    # shell command line (env K=V ... claude ...) only a real shell can parse.
    assert runner.calls_for("respawn-pane") == [
        ["tmux", "respawn-pane", "-k", "-t", "agents:1.0", "bash", "-lc", "claude --foo"]
    ]


def test_tmux_ensure_worker_raises_without_spawn_cmd_when_dead():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd=None)
    runner.script("display-message", stdout="bash\n")
    with pytest.raises(RuntimeError):
        adapter.ensure_worker()
    assert not runner.calls_for("respawn-pane")


def test_tmux_is_idle_requires_stability_window():
    adapter, runner, clock = make_tmux_adapter(idle_quiet_seconds=10)
    runner.script("capture-pane", stdout="same output\n")

    assert adapter.is_idle() is False   # first observation just establishes the baseline
    clock.advance(5)
    assert adapter.is_idle() is False   # stable, but not long enough yet
    clock.advance(6)
    assert adapter.is_idle() is True    # stable >= idle_quiet_seconds now


def test_tmux_is_idle_resets_on_output_change():
    adapter, runner, clock = make_tmux_adapter(idle_quiet_seconds=10)
    runner.script("capture-pane", stdout="A\n")
    adapter.is_idle()
    clock.advance(11)
    assert adapter.is_idle() is True

    runner.script("capture-pane", stdout="B\n")   # new output -> stability resets
    assert adapter.is_idle() is False
    clock.advance(11)
    assert adapter.is_idle() is True


def test_tmux_is_idle_false_on_capture_failure():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", returncode=1, stderr="no server running on socket")
    assert adapter.is_idle() is False


# --------------------------------------------------------------------------- #
# QA fix (finding 1): completion_marker() is now an adapter-LOCAL monotonic
# counter (never None -- raises on capture failure only), immune to
# capture-pane scrollback truncation regressing it.
# --------------------------------------------------------------------------- #

def test_tmux_completion_marker_returns_string_zero_not_none_when_no_markers_yet():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", stdout="just some ordinary chatter\n")
    marker = adapter.completion_marker()
    assert marker == "0"
    assert isinstance(marker, str)


def test_tmux_completion_marker_increments_once_when_markers_first_appear():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", stdout="waiting...\n")
    baseline = adapter.completion_marker()
    assert baseline == "0"

    runner.script("capture-pane", stdout="waiting...\nTICK RESULT: NO WORK\n")
    after = adapter.completion_marker()
    assert after == "1"
    assert after != baseline


def test_tmux_completion_marker_monotonic_across_multiple_real_completions():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", stdout="TICK RESULT: NO WORK\n")
    m1 = adapter.completion_marker()
    runner.script("capture-pane", stdout="TICK RESULT: NO WORK\nTICK RESULT: WORKED #2\n")
    m2 = adapter.completion_marker()
    runner.script("capture-pane",
                  stdout="TICK RESULT: NO WORK\nTICK RESULT: WORKED #2\nTICK RESULT: NO WORK\n")
    m3 = adapter.completion_marker()
    assert [m1, m2, m3] == ["1", "2", "3"]


def test_tmux_completion_marker_truncation_count_drop_same_hash_is_no_op():
    # Simulates capture-pane's `-S -N` scrollback window sliding: an OLDER
    # marker (+ its context) falls off the front, but the NEWEST marker line
    # + its trailing context is byte-identical to what was already seen --
    # count regresses (2 -> 1) with an UNCHANGED hash. Must NOT register as
    # a change (that would be a false completion/regression), and must NOT
    # move the reference state either (so a later REAL increase is still
    # measured against the true, pre-truncation peak).
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane",
                  stdout="TICK RESULT: NO WORK\nsome context\nTICK RESULT: WORKED #1\n"
                         "trailing1\ntrailing2\n")
    first = adapter.completion_marker()
    assert first == "1"     # first-ever observation: 0 -> 2 markers registers as ONE bump

    runner.script("capture-pane",
                  stdout="TICK RESULT: WORKED #1\ntrailing1\ntrailing2\n")
    second = adapter.completion_marker()
    assert second == first == "1"   # truncation -- no new completion, no change

    # Prove the reference state truly wasn't touched: a genuine new marker
    # bringing the count back up to 2 (this time for real) still registers
    # as an increase relative to the ORIGINAL peak of 2, not the truncated 1.
    runner.script("capture-pane",
                  stdout="TICK RESULT: WORKED #1\ntrailing1\ntrailing2\nTICK RESULT: WORKED #3\n")
    third = adapter.completion_marker()
    assert third == "2"


def test_tmux_completion_marker_count_increase_registers_change_for_identical_marker_text():
    # The "identical-consecutive-NO-WORK" case: the marker line's own text
    # is byte-for-byte the same both times, so a hash-of-the-marker-line-
    # alone scheme would miss it -- but the raw COUNT still increases (a
    # second, genuinely new "TICK RESULT: NO WORK" line appeared), which
    # alone is sufficient to register a change.
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", stdout="TICK RESULT: NO WORK\n")
    first = adapter.completion_marker()
    assert first == "1"

    runner.script("capture-pane", stdout="TICK RESULT: NO WORK\nTICK RESULT: NO WORK\n")
    second = adapter.completion_marker()
    assert second == "2"
    assert second != first


def test_tmux_completion_marker_raises_on_capture_failure():
    # BLOCKER 1 contract (shared with OpencodeAdapter): raising here is what
    # lets the Sidecar map this to _BASELINE_UNKNOWN and recover later.
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", returncode=1, stderr="tmux server gone")
    with pytest.raises(RuntimeError):
        adapter.completion_marker()


def test_tmux_inject_uses_load_buffer_paste_buffer_then_enter_never_raw_text():
    # buffer name namespaced by project+agent_id (finding 4) -- see the
    # dedicated buffer-name tests below for the sanitization behavior.
    adapter, runner, clock = make_tmux_adapter(project="proj", agent_id=42)
    adapter.inject("do the thing " * 50)   # long text must never hit send-keys directly

    load_calls = runner.calls_for("load-buffer")
    paste_calls = runner.calls_for("paste-buffer")
    enter_calls = runner.calls_for("send-keys")
    assert len(load_calls) == 1
    assert load_calls[0][2:4] == ["-b", "sidecar-proj-42"]
    assert len(paste_calls) == 1
    assert "-d" in paste_calls[0] and "-t" in paste_calls[0] and "agents:1.0" in paste_calls[0]
    assert len(enter_calls) == 1
    assert enter_calls[0][-1] == "Enter"
    assert not any("do the thing" in " ".join(c) for c in enter_calls)


def test_tmux_inject_raises_when_load_buffer_fails():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("load-buffer", returncode=1, stderr="no such buffer file")
    with pytest.raises(RuntimeError):
        adapter.inject("text")


def test_tmux_clear_sends_slash_clear_via_send_keys_not_paste_buffer():
    adapter, runner, clock = make_tmux_adapter()
    adapter.clear()
    enter_calls = runner.calls_for("send-keys")
    assert enter_calls and enter_calls[-1][-2:] == ["/clear", "Enter"]
    assert not runner.calls_for("paste-buffer")


def test_tmux_clear_raises_on_send_keys_failure():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("send-keys", returncode=1, stderr="no pane")
    with pytest.raises(RuntimeError):
        adapter.clear()


def test_tmux_clear_resets_usage_and_idle_tracking():
    adapter, runner, clock = make_tmux_adapter()
    adapter.inject("x" * 40)
    assert adapter.get_usage()["context_tokens"] > 0
    runner.script("capture-pane", stdout="steady\n")
    adapter.is_idle()

    adapter.clear()

    assert adapter.get_usage()["context_tokens"] == 0
    assert adapter.last_output_change() is None


def test_tmux_restart_requires_spawn_cmd():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd=None)
    with pytest.raises(RuntimeError):
        adapter.restart()
    assert not runner.calls_for("respawn-pane")


def test_tmux_restart_respawns_and_resets_state():
    adapter, runner, clock = make_tmux_adapter(spawn_cmd="claude --resume")
    adapter.inject("hello")
    adapter.restart()
    assert runner.calls_for("respawn-pane") == [
        ["tmux", "respawn-pane", "-k", "-t", "agents:1.0", "bash", "-lc", "claude --resume"]
    ]
    assert adapter.get_usage()["context_tokens"] == 0


def test_tmux_respawn_passes_bash_lc_as_separate_argv_no_extra_quoting():
    # finding 6: bash/-lc/spawn_cmd must be THREE separate argv items handed
    # straight to the tmux runner -- no additional shell-quoting applied to
    # spawn_cmd here (that would double-escape whatever quoting spawn_cmd
    # itself already carries, e.g. from --print-cmd's %q output).
    spawn = 'env MCP_TOOL_TIMEOUT=3300000 claude --dangerously-skip-permissions "hello world"'
    adapter, runner, clock = make_tmux_adapter(spawn_cmd=spawn)
    runner.script("display-message", stdout="bash\n")
    adapter.ensure_worker()
    call = runner.calls_for("respawn-pane")[0]
    assert call[-3:] == ["bash", "-lc", spawn]   # spawn_cmd passed through byte-for-byte


def test_tmux_get_usage_monotonic_across_inject_and_read_result():
    adapter, runner, clock = make_tmux_adapter()
    adapter.inject("12345678")  # 8 chars
    u1 = adapter.get_usage()
    runner.script("capture-pane", stdout="A" * 40)
    adapter.read_result()
    u2 = adapter.get_usage()
    assert u2["context_tokens"] >= u1["context_tokens"]
    assert u2["session_cost"] == 0.0


def test_tmux_read_result_returns_tail_lines_and_none_on_empty():
    adapter, runner, clock = make_tmux_adapter(result_lines=2)
    runner.script("capture-pane", stdout="l1\nl2\nl3\n")
    assert adapter.read_result() == "l2\nl3"

    runner.script("capture-pane", stdout="")
    assert adapter.read_result() is None


def test_tmux_read_result_returns_none_on_capture_failure():
    adapter, runner, clock = make_tmux_adapter()
    runner.script("capture-pane", returncode=1, stderr="gone")
    assert adapter.read_result() is None


def test_tmux_shutdown_kills_pane_only_when_requested():
    adapter, runner, clock = make_tmux_adapter()
    adapter.shutdown(kill_worker=False)
    assert not runner.calls_for("kill-pane")
    adapter.shutdown(kill_worker=True)
    assert runner.calls_for("kill-pane")


def test_cli_tmux_runtime_requires_tmux_target():
    with pytest.raises(SystemExit):
        sidecar.main(["--agent-id", "1", "--project", "p", "--dashboard", "http://x",
                      "--prompt-file", str(Path(__file__)), "--runtime", "tmux"])


# --------------------------------------------------------------------------- #
# Phase 5: opencode balance/credit-exhaustion alert (plan §6)
# --------------------------------------------------------------------------- #

def test_balance_alert_fires_pauses_and_posts_alert(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)                                    # tick #1 injected
    adapter.set_last_error("APIError", "insufficient balance for this request")
    adapter.complete("TICK RESULT: NO WORK (balance)")
    sc.step(clock.t)                                     # collect -> balance check runs

    assert dashboard.pause_calls == [{"minutes": 120}]
    assert len(dashboard.alert_calls) == 1
    assert "insufficient balance" in dashboard.alert_calls[0]["body"].lower()
    out = capsys.readouterr().out
    assert "BALANCE_ALERT" in out


def test_balance_alert_no_match_does_not_fire():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.set_last_error("SomeOtherError", "the model timed out")
    adapter.complete("TICK RESULT: NO WORK (other)")
    sc.step(clock.t)
    assert dashboard.pause_calls == []
    assert dashboard.alert_calls == []


def test_balance_alert_rate_limited_to_once_per_hour():
    # Exercises Sidecar._check_balance_alert directly (rather than round-
    # tripping full ticks across an hour of fake clock, which would also
    # cross the 30-min active-window and go DORMANT -- an orthogonal
    # mechanism this test isn't about): the cooldown itself is what's
    # under test.
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.set_last_error("APIError", "insufficient credit")

    sc._check_balance_alert(clock.t)
    assert len(dashboard.pause_calls) == 1
    assert len(dashboard.alert_calls) == 1

    # Still erroring, well within the hour -- must NOT re-fire.
    clock.advance(300)
    sc._check_balance_alert(clock.t)
    assert len(dashboard.pause_calls) == 1
    assert len(dashboard.alert_calls) == 1

    # An hour later, still erroring -- fires again.
    clock.advance(3601)
    sc._check_balance_alert(clock.t)
    assert len(dashboard.pause_calls) == 2
    assert len(dashboard.alert_calls) == 2


def test_balance_alert_does_not_enter_dormant():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.set_last_error("APIError", "insufficient quota")
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.t)
    assert sc.state == "ACTIVE"     # no dormant-until-reset: cost exhaustion has no rollover


def test_balance_alert_checked_after_inject_error_too():
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.raise_on_inject = True
    adapter.set_last_error("APIError", "payment required (402)")
    sc.step(clock.t)                # inject fails -> INJECT_ERROR -> balance check runs

    assert dashboard.pause_calls == [{"minutes": 120}]
    assert len(dashboard.alert_calls) == 1


def test_balance_alert_last_error_raising_is_swallowed():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.raise_on_last_error = True
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.t)                 # must not raise; no pause/alert either
    assert dashboard.pause_calls == []
    assert dashboard.alert_calls == []


def test_balance_alert_none_when_no_error():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.t)
    assert dashboard.pause_calls == []
    assert dashboard.alert_calls == []


# --------------------------------------------------------------------------- #
# BUGFIX (coordinator live-tmux e2e report): restart cooldown. Nothing
# bounded restart FREQUENCY -- a persistent alive()-false (or a crash-
# looping TUI) restarted every ~3 probes, seconds apart. For a real
# claude/codex session each respawn replays the initial prompt (token burn +
# session churn). _restart() now enforces a minimum number of seconds
# between actual restarts; a restart that would fire within the cooldown is
# logged (RESTART_SUPPRESSED) and skipped WITHOUT resetting the counters
# that triggered it, so it fires as soon as the cooldown lapses.
# --------------------------------------------------------------------------- #

def test_restart_cooldown_default_matches_documented_constant():
    sc, adapter, dashboard, clock = make_sidecar()
    assert sc.restart_cooldown == sidecar.DEFAULT_RESTART_COOLDOWN_S == 120


def test_restart_cooldown_limits_persistent_alive_failure_storm(capsys):
    sc, adapter, dashboard, clock = make_sidecar(restart_cooldown=10)
    adapter.alive_flag = False   # persistently dead: every probe fails, forever

    for _ in range(13):
        clock.advance(1)
        sc.step(clock.t)

    # t=3: 3rd consecutive failure -- first restart, never suppressed (no
    #   prior restart to be within cooldown of).
    # t=6..12: threshold re-reached every step thereafter (the failure
    #   counter is never reset by a SUPPRESSED restart), but each is still
    #   within the 10s cooldown of the t=3 restart -> 7 suppressed events.
    # t=13: 13-3=10 >= cooldown -> fires for real (2nd restart).
    assert adapter.restart_count == 2
    out = capsys.readouterr().out
    assert out.count("RESTART_SUPPRESSED") == 7

    for _ in range(10):
        clock.advance(1)
        sc.step(clock.t)
    # The storm continues (still persistently dead) -- exactly one more
    # restart lands per cooldown window, not one per 3-probe threshold.
    assert adapter.restart_count == 3


def test_restart_cooldown_zero_disables_suppression_immediate_behavior():
    sc, adapter, dashboard, clock = make_sidecar(restart_cooldown=0)
    adapter.alive_flag = False

    for _ in range(9):     # 3 threshold-crossings, 3 probes apart each
        clock.advance(1)
        sc.step(clock.t)

    # cooldown=0 restores the pre-fix behavior: every threshold-crossing
    # restarts immediately, none suppressed.
    assert adapter.restart_count == 3


def test_restart_cooldown_zero_never_logs_suppressed(capsys):
    sc, adapter, dashboard, clock = make_sidecar(restart_cooldown=0)
    adapter.alive_flag = False
    for _ in range(9):
        clock.advance(1)
        sc.step(clock.t)
    out = capsys.readouterr().out
    assert "RESTART_SUPPRESSED" not in out


def test_restart_cooldown_applies_to_owned_process_dead_fast_path(capsys):
    # The owned-process-dead path bypasses the alive()-flap DEBOUNCE (it's a
    # confirmed-dead fast path, not a flaky probe), but it must still go
    # through the same cooldown as every other restart trigger -- otherwise
    # a persistently-dead owned subprocess would restart-storm every step.
    sc, adapter, dashboard, clock = make_sidecar(restart_cooldown=50)
    adapter.owns_process = True
    adapter.proc_dead = True     # permanently "confirmed dead"

    sc.step(clock.t)                       # t=0: first restart, no cooldown yet
    assert adapter.restart_count == 1

    clock.advance(10)
    sc.step(clock.t)                       # t=10: elapsed 10 < 50 -> suppressed
    assert adapter.restart_count == 1
    out = capsys.readouterr().out
    assert "RESTART_SUPPRESSED" in out

    clock.advance(50)
    sc.step(clock.t)                       # t=60: elapsed 60 >= 50 -> fires again
    assert adapter.restart_count == 2


def test_cli_rejects_negative_restart_cooldown(capsys):
    with pytest.raises(SystemExit):
        sidecar.main(["--agent-id", "1", "--project", "p", "--dashboard", "http://x",
                      "--prompt-file", str(Path(__file__)), "--runtime", "opencode",
                      "--opencode-url", "http://127.0.0.1:4096", "--restart-cooldown", "-1"])
    err = capsys.readouterr().err
    assert "--restart-cooldown" in err


def test_tmux_full_tick_cycle_with_healthy_shell_driven_worker_no_restarts(capsys):
    """End-to-end sanity check (coordinator e2e follow-up): with the alive()
    fix in place, a healthy bash-driven fake-TUI (pane_current_command ==
    "bash" throughout -- exactly the coordinator's live e2e shape) completes
    a full inject -> marker-change -> collect cycle with ZERO restarts. The
    coordinator's e2e storm (66 ALIVE_PROBE_FAIL / 33 RESTART / 0
    TICK_RESULT in 100s) was entirely a byproduct of alive() misreporting a
    healthy shell-driven worker as dead -- this is the real Sidecar +
    TmuxAdapter (not FakeAdapter) proving collection works end to end once
    that's fixed."""
    runner = FakeTmuxRunner()
    clock = FakeClock(0.0)
    runner.script("display-message", stdout="bash\n")   # healthy, shell-driven, throughout
    runner.script("capture-pane", stdout="waiting for input\n")

    adapter = sidecar.TmuxAdapter(
        tmux_target="agents:9.0", spawn_cmd="bash /path/to/fake-tui.sh",
        project="proj", agent_id=99, idle_quiet_seconds=5,
        tmux_runner=runner, clock=clock,
    )
    dashboard = FakeDashboard()
    sc = sidecar.Sidecar(
        adapter=adapter, dashboard=dashboard, worker_prompt="WORKER PROMPT",
        tick_contract="TICK CONTRACT", clock=clock, sleeper=noop_sleeper, t_max=3600,
    )

    # Let the first tick get injected and settle as "in flight, no result yet".
    # (TmuxAdapter has no restart_count of its own -- restarts are observed
    # through the fake tmux runner's respawn-pane call log instead.)
    for _ in range(8):
        clock.advance(1)
        sc.step(clock.t)
    assert not runner.calls_for("respawn-pane")
    assert sc.tick_start_at is not None

    # Worker finishes and prints its result -- a new TICK RESULT line
    # (marker change), then holds stable long enough for is_idle() to trip.
    runner.script("capture-pane",
                  stdout="waiting for input\n$ \nTICK RESULT: WORKED #11; READY-TO-CLEAR\n")
    for _ in range(15):
        clock.advance(1)
        sc.step(clock.t)

    assert not runner.calls_for("respawn-pane")  # still zero -- no false "dead" detection
    out = capsys.readouterr().out
    assert "event=TICK_RESULT" in out and "worked=True" in out and "ids=[11]" in out
    clear_calls = [c for c in runner.calls_for("send-keys") if c[-2:] == ["/clear", "Enter"]]
    assert len(clear_calls) >= 1                # READY-TO-CLEAR handshake fired


# --------------------------------------------------------------------------- #
# QA fix (finding 4): paste/load-buffer names are namespaced by (project,
# agent_id), sanitized to tmux/shell-safe characters.
# --------------------------------------------------------------------------- #

def test_tmux_buffer_name_sanitizes_project_and_includes_agent_id():
    adapter, runner, clock = make_tmux_adapter(project="cadence/lms prod!", agent_id=3)
    adapter.inject("x")
    buf_name = runner.calls_for("load-buffer")[0][3]
    assert buf_name == "sidecar-cadencelmsprod-3"
    assert re.fullmatch(r"[a-zA-Z0-9-]+", buf_name)


def test_tmux_buffer_name_falls_back_to_x_when_project_missing():
    adapter, runner, clock = make_tmux_adapter(project=None, agent_id=5)
    adapter.inject("x")
    assert runner.calls_for("load-buffer")[0][3] == "sidecar-x-5"


# --------------------------------------------------------------------------- #
# QA fix (finding 3): OpencodeAdapter._ensure_session() verifies the
# resolved/created session is actually scoped to self.directory, raising
# loudly on a positive mismatch (never on a merely-inconclusive check).
# --------------------------------------------------------------------------- #

def _make_opencode_adapter_with_fake_request(directory="/work/projA", request_fn=None):
    adapter = sidecar.OpencodeAdapter(base_url="http://127.0.0.1:4999", directory=directory,
                                       project="projA", agent_id=1)
    if request_fn is not None:
        adapter._request = request_fn
    return adapter


def test_opencode_ensure_session_raises_on_directory_mismatch():
    def fake_request(method, path, payload=None, timeout=None):
        if method == "GET" and path.startswith("/session?directory="):
            return []
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        if method == "GET" and path == "/session/ses_new":
            return {"id": "ses_new", "directory": "/work/projB"}   # WRONG directory
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(request_fn=fake_request)

    with pytest.raises(RuntimeError, match="different directory"):
        adapter._ensure_session()


def test_opencode_ensure_session_passes_on_directory_match():
    def fake_request(method, path, payload=None, timeout=None):
        if method == "GET" and path.startswith("/session?directory="):
            return []
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        if method == "GET" and path == "/session/ses_new":
            return {"id": "ses_new", "directory": "/work/projA"}   # matches
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(request_fn=fake_request)

    adapter._ensure_session()   # must not raise
    assert adapter.session_id == "ses_new"


def test_opencode_ensure_session_ownership_falls_back_to_directory_listing_when_field_absent():
    # Some server versions may not expose `directory` on the Session object
    # -- fall back to confirming our id shows up in the SAME
    # directory-scoped listing _ensure_session already uses for title
    # re-discovery.
    calls = {"listing": 0}

    def fake_request(method, path, payload=None, timeout=None):
        if method == "GET" and path.startswith("/session?directory="):
            calls["listing"] += 1
            if calls["listing"] == 1:
                return []                        # title-discovery: nothing yet
            return [{"id": "ses_new"}]             # ownership fallback: WE are listed
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        if method == "GET" and path == "/session/ses_new":
            return {"id": "ses_new"}               # no `directory` field at all
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(request_fn=fake_request)

    adapter._ensure_session()   # must not raise
    assert adapter.session_id == "ses_new"


def test_opencode_ensure_session_ownership_fallback_raises_when_id_not_listed():
    def fake_request(method, path, payload=None, timeout=None):
        if method == "GET" and path.startswith("/session?directory="):
            return []   # neither title-discovery nor the ownership fallback finds us
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        if method == "GET" and path == "/session/ses_new":
            return {"id": "ses_new"}   # no `directory` field
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(request_fn=fake_request)

    with pytest.raises(RuntimeError, match="different directory"):
        adapter._ensure_session()


def test_opencode_ensure_session_ownership_check_degrades_gracefully_on_transient_failure():
    # BOTH the primary (GET /session/{id}) and fallback (GET
    # /session?directory=) verification calls fail (network blip) -- this
    # must NOT crash a session that may be perfectly fine; only a POSITIVE
    # mismatch raises.
    def fake_request(method, path, payload=None, timeout=None):
        if method == "GET" and path.startswith("/session?directory="):
            raise RuntimeError("network blip")
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        if method == "GET" and path == "/session/ses_new":
            raise RuntimeError("network blip")
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(request_fn=fake_request)

    adapter._ensure_session()   # must NOT raise
    assert adapter.session_id == "ses_new"


def test_opencode_ownership_check_skipped_when_no_directory_configured():
    called = []

    def fake_request(method, path, payload=None, timeout=None):
        called.append((method, path))
        if method == "POST" and path == "/session":
            return {"id": "ses_new"}
        raise AssertionError(f"unexpected request {method} {path}")
    adapter = _make_opencode_adapter_with_fake_request(directory=None, request_fn=fake_request)

    adapter._ensure_session()
    assert adapter.session_id == "ses_new"
    assert not any(p.startswith("/session/ses_new") for _, p in called)   # no ownership GET at all


# --------------------------------------------------------------------------- #
# QA fix (finding 5): the READY-TO-CLEAR drain re-inject is no longer
# unconditional -- it flows through the SAME idle-gated path as every other
# tick. Opencode-shaped adapters (idle immediately after clear()) still
# drain same-step; a worker that stays busy right after clear() must wait.
# --------------------------------------------------------------------------- #

def test_ready_to_clear_drain_waits_for_idle_when_worker_not_idle_after_clear():
    class SlowClearAdapter(FakeAdapter):
        def clear(self):
            super().clear()
            self.busy = True   # simulate "still settling" right after clear() (e.g. tmux post-/clear)

    adapter = SlowClearAdapter()
    sc, adapter, dashboard, clock = make_sidecar(adapter=adapter)

    sc.step(clock.t)                                          # tick #1 injected
    adapter.complete("TICK RESULT: WORKED #1; READY-TO-CLEAR")
    sc.step(clock.t)                                            # collect -> clear() -> now busy
    assert adapter.clear_count == 1
    assert len(adapter.injected) == 1        # NOT re-injected yet -- worker reads busy
    assert sc.pending is True

    adapter.busy = False                      # worker settles
    sc.step(clock.advance(0.1))
    assert len(adapter.injected) == 2          # NOW delivered, via the normal idle-gated path


# --------------------------------------------------------------------------- #
# QA fix (finding 7a): result collection now gates on completion-MARKER
# stability (same value on two consecutive reads), not whole-tail is_idle().
# --------------------------------------------------------------------------- #

def test_collection_requires_marker_stable_across_two_consecutive_reads():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)                       # tick #1 injected
    adapter.complete("TICK RESULT: WORKED #1")
    adapter.marker_flip_remaining = 1       # this step's first read disagrees with its own confirm read
    sc.step(clock.t)
    assert sc.tick_start_at is not None       # NOT collected yet -- the two reads disagreed

    sc.step(clock.t)                          # both reads now agree -> collects
    assert sc.tick_start_at is None
    assert sc.last_worked_at == clock.t


def test_collection_unaffected_when_reads_agree_immediately():
    # The overwhelmingly common case (and every pre-existing single-step
    # collection test in this file) -- two consecutive reads that already
    # agree collect on the very same step, no extra step needed.
    sc, adapter, dashboard, clock = make_sidecar()
    sc.step(clock.t)
    adapter.complete("TICK RESULT: WORKED #1")
    sc.step(clock.t)
    assert sc.tick_start_at is None
    assert sc.last_worked_at == clock.t


# --------------------------------------------------------------------------- #
# QA fix (finding 7b): idle-wait fallback for INJECTION -- if a due tick has
# been undeliverable purely because is_idle() won't settle (animated-TUI
# hash instability) for > 3x idle_quiet_seconds, AND the completion marker
# hasn't moved at all in that time, inject anyway.
# --------------------------------------------------------------------------- #

def test_idle_fallback_injects_after_prolonged_hash_instability_with_unchanged_marker(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.idle_quiet_seconds = 5     # only this test's adapter instance opts in
    adapter.force_not_idle = True       # is_idle() never settles (simulated hash instability)

    sc.step(clock.t)                    # t=0: tick due, but not idle -- wait starts
    assert len(adapter.injected) == 0
    assert sc.pending is True

    for _ in range(14):                 # t=1..14: marker stays unchanged (nothing has completed)
        clock.advance(1)
        sc.step(clock.t)
    assert len(adapter.injected) == 0     # elapsed 14s < 3*5=15s -- must NOT fall back yet

    clock.advance(2)                    # t=16: elapsed 16s > 15s
    sc.step(clock.t)
    assert len(adapter.injected) == 1     # fallback fired
    out = capsys.readouterr().out
    assert "IDLE_FALLBACK" in out


def test_idle_fallback_resets_when_marker_changes_during_the_wait():
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.idle_quiet_seconds = 5
    adapter.force_not_idle = True

    sc.step(clock.t)                    # t=0: wait starts (marker unknown/None)
    for _ in range(10):                 # t=1..10: marker still unchanged
        clock.advance(1)
        sc.step(clock.t)
    assert len(adapter.injected) == 0

    # Something genuinely changes (a completion happened) -- the wait must
    # reset, even though is_idle() is still forced False the whole time.
    adapter.complete("TICK RESULT: WORKED #1")
    for _ in range(10):                 # t=11 (reset here) .. t=20
        clock.advance(1)
        sc.step(clock.t)
    assert len(adapter.injected) == 0     # elapsed since the reset (9s) not yet > 15s

    clock.advance(7)                    # t=27: elapsed since reset (16s) > 15s
    sc.step(clock.t)
    assert len(adapter.injected) == 1


def test_idle_fallback_disabled_for_adapters_without_idle_quiet_seconds():
    # opencode-shaped adapters (no idle_quiet_seconds attribute) never get
    # the fallback -- is_idle() there is an instantaneous HTTP fact, not a
    # hash-stability heuristic with anything to bound damage from.
    sc, adapter, dashboard, clock = make_sidecar()
    adapter.force_not_idle = True
    assert not hasattr(adapter, "idle_quiet_seconds")

    sc.step(clock.t)
    for _ in range(100):
        clock.advance(1)
        sc.step(clock.t)
    assert len(adapter.injected) == 0     # never falls back, waits forever


# --------------------------------------------------------------------------- #
# §15: orchestrator-authoritative work signal (check_work_signal)
# --------------------------------------------------------------------------- #

def test_work_signal_first_observation_establishes_baseline_without_firing(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.protocol_violations = 2
    worked_before = sc.last_worked_at
    capsys.readouterr()
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})
    assert sc._last_work_at is not None
    assert sc.last_worked_at == worked_before          # window NOT reset on baseline
    assert sc.protocol_violations == 2                 # not cleared on baseline
    assert "event=WORK_SIGNAL" not in capsys.readouterr().out


def test_work_signal_increase_resets_window_and_clears_violations(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})   # baseline
    sc.protocol_violations = 3
    sc.pending = False
    clock.advance(500)
    capsys.readouterr()
    sc.check_work_signal({"last_work_at": "2026-01-01T00:05:00+00:00"})   # greater -> fires
    assert "event=WORK_SIGNAL" in capsys.readouterr().out
    assert sc.last_worked_at == clock.t                # active window reset to now
    assert sc.protocol_violations == 0                 # violations cleared
    assert sc.pending is False                         # does NOT force a tick (unlike wake)


def test_work_signal_equal_value_does_not_fire():
    sc, adapter, dashboard, clock = make_sidecar()
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})   # baseline
    sc.protocol_violations = 4
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})   # equal -> no-op
    assert sc.protocol_violations == 4


def test_work_signal_wakes_from_dormant(capsys):
    sc, adapter, dashboard, clock = make_sidecar()
    sc.state = "DORMANT"
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})   # baseline, no fire
    assert sc.state == "DORMANT"
    capsys.readouterr()
    sc.check_work_signal({"last_work_at": "2026-01-01T00:05:00+00:00"})   # increase
    assert sc.state == "ACTIVE"
    assert "event=WORK_SIGNAL" in capsys.readouterr().out


def test_work_signal_keeps_active_window_alive_despite_invalid_ticks():
    """§15 headline: a worker doing real work (orchestrator signal advances) never
    hits window_elapsed even when it never emits a valid TICK RESULT marker."""
    sc, adapter, dashboard, clock = make_sidecar(active_window=1800)
    sc.check_work_signal({"last_work_at": "2026-01-01T00:00:00+00:00"})   # baseline @ t=0
    clock.advance(1000)
    sc.check_work_signal({"last_work_at": "2026-01-01T00:16:00+00:00"})   # advances window
    assert sc.last_worked_at == clock.t                                   # 1000
    # 1801s past the ORIGINAL window-from-0, but only 801s since the work signal
    # reset it -> still ACTIVE, not window_elapsed.
    clock.advance(801)
    sc.state = "ACTIVE"
    sc._check_window(clock.t)
    assert sc.state == "ACTIVE"


def test_state_poll_relays_last_work_at_end_to_end():
    """_maybe_poll_state feeds the fetched payload into check_work_signal."""
    sc, adapter, dashboard, clock = make_sidecar(state_poll_interval=45)
    sc.step(clock.t)                       # first poll; last_work_at still None
    adapter.complete("TICK RESULT: NO WORK")
    sc.step(clock.advance(0.1))

    dashboard.last_work_at = "2026-01-01T00:00:00+00:00"
    clock.advance(50)
    sc.step(clock.t)                       # baseline observation, no fire
    assert sc._last_work_at is not None

    dashboard.last_work_at = "2026-01-01T00:05:00+00:00"
    sc.protocol_violations = 2
    clock.advance(50)
    worked_before = sc.last_worked_at
    sc.step(clock.t)                       # increase -> fires through _maybe_poll_state
    assert sc.last_worked_at >= worked_before
    assert sc.protocol_violations == 0
