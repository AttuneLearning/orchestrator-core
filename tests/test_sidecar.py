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
import signal
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

        # -- Phase 3: scripted usage + session-id tracking ---------------
        # `usage_queue` is consumed one entry per get_usage() call (FIFO);
        # once exhausted, the last value keeps being returned (mirrors a
        # real adapter reporting a steady reading), starting from None
        # (unknown) if the queue was never populated.
        self.usage_queue: list[dict | None] = []
        self._last_usage: dict | None = None
        self._session_counter = 0
        self.session_id = f"sess-{self._session_counter}"

    def ensure_worker(self) -> None:
        pass

    def is_idle(self) -> bool:
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
        return str(self._completion_counter) if self._completion_counter else None

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
        return payload


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

def test_opencode_adapter_get_usage_prefers_total_when_present():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    adapter._request = lambda method, path, payload=None, timeout=None: {
        "tokens": {"input": 100, "output": 50, "reasoning": 20,
                   "cache": {"read": 10, "write": 5}, "total": 999},
        "cost": 1.23,
    }
    assert adapter.get_usage() == {"context_tokens": 999, "session_cost": 1.23}


def test_opencode_adapter_get_usage_sums_all_fields_including_cache_write_and_reasoning():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    # No `total` field -> must fall back to summing ALL parts, including
    # cache.write (15) and reasoning (20) -- the two fields the pre-QA-fix
    # code silently dropped.
    adapter._request = lambda method, path, payload=None, timeout=None: {
        "tokens": {"input": 100, "output": 50, "reasoning": 20,
                   "cache": {"read": 10, "write": 15}},
        "cost": 0.5,
    }
    usage = adapter.get_usage()
    assert usage == {"context_tokens": 195, "session_cost": 0.5}   # 100+50+20+10+15


def test_opencode_adapter_get_usage_falls_back_to_sum_when_total_missing_or_zero():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"
    adapter._request = lambda method, path, payload=None, timeout=None: {
        "tokens": {"input": 10, "output": 5, "reasoning": 0,
                   "cache": {"read": 0, "write": 0}, "total": 0},
        "cost": 0.0,
    }
    assert adapter.get_usage() == {"context_tokens": 15, "session_cost": 0.0}


def test_opencode_adapter_get_usage_returns_none_on_request_failure():
    adapter = sidecar.OpencodeAdapter(base_url="http://fake", project="p", agent_id=1)
    adapter.session_id = "ses_1"

    def _raise(*a, **kw):
        raise RuntimeError("boom")
    adapter._request = _raise
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
