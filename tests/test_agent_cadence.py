"""Engine-side tests for Phase 4 of the durable-worker-sidecar plan (migration
0024, plan §7): cadence-window fields, heartbeat status, and the per-project
wake signal. Covers repository.touch_agent/set_agent_loop/get_wake_at/bump_wake,
the dashboard pause/heartbeat/wake endpoints, monitoring.agents_with_staleness's
dormant exemption, and the engine reclaim sweep's dormant/freshness guard.
"""

from __future__ import annotations

import copy
import warnings

warnings.filterwarnings("ignore")

import pytest
from fastapi.testclient import TestClient

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.dashboard.app import create_app
from orchestrator.engine.loop import Engine
from orchestrator.monitoring import agents_with_staleness


# --------------------------------------------------------------------------- #
# repository.touch_agent — status mapping
# --------------------------------------------------------------------------- #

def test_touch_agent_no_status_is_byte_identical_to_pre_migration_behavior(pool):
    """Old callers (run-agent-loop.sh curl, MCP heartbeat/list_my_work/my_queue)
    never pass `status` — the revive-only behavior must be untouched."""
    a = repo.register_agent(pool, "backend", "dev")
    repo.set_agent_status(pool, a.id, "offline")

    repo.touch_agent(pool, a.id)

    refreshed = repo.get_agent(pool, a.id)
    assert refreshed.status == "idle"          # revived
    assert refreshed.last_seen is not None


def test_touch_agent_no_status_never_demotes_busy(pool):
    a = repo.register_agent(pool, "backend", "dev")
    repo.set_agent_status(pool, a.id, "busy")

    repo.touch_agent(pool, a.id)

    assert repo.get_agent(pool, a.id).status == "busy"


@pytest.mark.parametrize("status, expected_db_status", [
    ("working", "busy"),
    ("idle", "idle"),
    ("dormant", "dormant"),
])
def test_touch_agent_status_mapping(pool, status, expected_db_status):
    a = repo.register_agent(pool, "backend", "dev")

    repo.touch_agent(pool, a.id, status=status)

    assert repo.get_agent(pool, a.id).status == expected_db_status


def test_touch_agent_dormant_agent_receiving_working_becomes_busy(pool):
    """The status param sets the value VERBATIM after mapping — it is not a
    mere revive, so it overrides even a non-offline prior status."""
    a = repo.register_agent(pool, "backend", "dev")
    repo.touch_agent(pool, a.id, status="dormant")
    assert repo.get_agent(pool, a.id).status == "dormant"

    repo.touch_agent(pool, a.id, status="working")

    assert repo.get_agent(pool, a.id).status == "busy"


def test_touch_agent_invalid_status_falls_back_to_revive_only_behavior(pool):
    a = repo.register_agent(pool, "backend", "dev")
    repo.set_agent_status(pool, a.id, "offline")

    repo.touch_agent(pool, a.id, status="bogus")

    assert repo.get_agent(pool, a.id).status == "idle"    # revived, not crashed


# --------------------------------------------------------------------------- #
# repository.set_agent_loop — new cadence-window bounds
# --------------------------------------------------------------------------- #

def test_set_agent_loop_cadence_window_fields_within_bounds(pool):
    a = repo.register_agent(pool, "backend", "dev")

    updated = repo.set_agent_loop(pool, a.id, active_window_seconds=600,
                                  dormant_interval_seconds=7200)

    assert updated.active_window_seconds == 600
    assert updated.dormant_interval_seconds == 7200


def test_set_agent_loop_rejects_active_window_seconds_out_of_bounds(pool):
    a = repo.register_agent(pool, "backend", "dev")
    with pytest.raises(ValueError):
        repo.set_agent_loop(pool, a.id, active_window_seconds=299)     # < 300
    with pytest.raises(ValueError):
        repo.set_agent_loop(pool, a.id, active_window_seconds=14401)   # > 14400


def test_set_agent_loop_rejects_dormant_interval_seconds_out_of_bounds(pool):
    a = repo.register_agent(pool, "backend", "dev")
    with pytest.raises(ValueError):
        repo.set_agent_loop(pool, a.id, dormant_interval_seconds=599)     # < 600
    with pytest.raises(ValueError):
        repo.set_agent_loop(pool, a.id, dormant_interval_seconds=86401)  # > 86400


def test_agent_defaults_active_window_and_dormant_interval(pool):
    a = repo.register_agent(pool, "backend", "dev")
    assert a.active_window_seconds == 1800
    assert a.dormant_interval_seconds == 3600


# --------------------------------------------------------------------------- #
# repository.get_wake_at / bump_wake — roundtrip
# --------------------------------------------------------------------------- #

def test_wake_at_absent_until_bumped(pool):
    assert repo.get_wake_at(pool, "proj-a") is None


def test_bump_wake_roundtrip(pool):
    wake_at = repo.bump_wake(pool, "proj-a")
    assert repo.get_wake_at(pool, "proj-a") == wake_at


def test_bump_wake_is_monotonically_increasing_and_project_scoped(pool):
    first = repo.bump_wake(pool, "proj-a")
    second = repo.bump_wake(pool, "proj-a")
    assert second >= first
    # A different project has its own independent signal.
    assert repo.get_wake_at(pool, "proj-b") is None


# --------------------------------------------------------------------------- #
# monitoring.agents_with_staleness — dormant is never stale
# --------------------------------------------------------------------------- #

def test_dormant_agent_excluded_from_staleness(pool):
    a = repo.register_agent(pool, "backend", "dev")
    repo.touch_agent(pool, a.id, status="dormant")
    # Backdate last_seen far past STALE_AFTER_S (600s) -- if the staleness
    # check were status-blind (busy-based only, without the dormant
    # exemption), a stale *dormant* agent still would not be flagged (dormant
    # != busy) -- assert that explicitly here, not just implicitly via a busy
    # agent test elsewhere.
    with pool.connection() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = now() - interval '3600 seconds' WHERE id = %s",
            (a.id,),
        )

    rows = agents_with_staleness(pool)
    row = next(r for r in rows if r["id"] == a.id)
    assert row["status"] == "dormant"
    assert row["stale"] is False


def test_busy_agent_with_stale_heartbeat_is_flagged(pool):
    a = repo.register_agent(pool, "backend", "dev")
    repo.touch_agent(pool, a.id, status="working")   # -> busy
    with pool.connection() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = now() - interval '3600 seconds' WHERE id = %s",
            (a.id,),
        )

    rows = agents_with_staleness(pool)
    row = next(r for r in rows if r["id"] == a.id)
    assert row["status"] == "busy"
    assert row["stale"] is True


# --------------------------------------------------------------------------- #
# engine/loop.py reclaim — dormant + fresh never reclaimed; stale reclaimed
# regardless of the agent's last-claimed status.
# --------------------------------------------------------------------------- #

def _engine(settings, pool, **threshold_overrides):
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=StubReasoner())


def _pull_issue(pool):
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "i", pipeline="pull-1", team="backend")
    repo.register_agent(pool, "backend", "lead", "api")
    return goal, issue


def test_dormant_agent_with_fresh_heartbeat_is_not_reclaimed(settings, pool):
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    repo.register_agent(pool, "backend", "dev", "external")   # a live replacement exists
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=5)

    eng.run(max_ticks=20)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    # dev1's side-car goes dormant but keeps heartbeating (touch_agent every
    # ~20s regardless of state) -- last_seen stays fresh.
    repo.touch_agent(pool, dev1.id, status="dormant")
    eng.run(max_ticks=20)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.assigned_agent == dev1.id                 # still its work
    assert repo.get_agent(pool, dev1.id).status == "dormant"   # never flipped offline
    events = [e.event_type for e in repo.recent_events(pool, issue.id, limit=200)]
    assert "reclaimed" not in events


def test_stale_dormant_agent_is_reclaimed_regardless_of_claimed_status(settings, pool):
    """A crashed side-car's last write could well have been 'dormant' -- once
    last_seen goes stale the agent is dead regardless of that claimed status,
    same as any other stale worker."""
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    dev2 = repo.register_agent(pool, "backend", "dev", "external")
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=5)

    eng.run(max_ticks=20)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    repo.touch_agent(pool, dev1.id, status="dormant")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = now() - interval '3600 seconds' WHERE id = %s",
            (dev1.id,),
        )
    eng.run(max_ticks=20)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.assigned_agent == dev2.id                  # reassigned
    assert repo.get_agent(pool, dev1.id).status == "offline"    # reclaimed, not dormant
    events = [e.event_type for e in repo.recent_events(pool, issue.id, limit=200)]
    assert "reclaimed" in events


# --------------------------------------------------------------------------- #
# Dashboard: pause-state payload, heartbeat status param, wake endpoint.
# --------------------------------------------------------------------------- #

def test_pause_endpoint_payload_includes_cadence_and_status_fields(settings, pool):
    a = repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.get(f"/agents/{a.id}/pause")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["active_window_seconds"] == 1800
    assert body["dormant_interval_seconds"] == 3600
    # No ?project= given -> resolves to the registry default key ("default"
    # for an injected-pool test app) -- null here simply because nothing has
    # bumped THAT key yet in this fresh test DB, not because the lookup was
    # skipped (see agent_pause_state: it always resolves via
    # context.current_key(), never conditionally on the raw param).
    assert body["wake_at"] is None


def test_pause_endpoint_surfaces_wake_at_for_project(settings, pool):
    a = repo.register_agent(pool, "backend", "dev")
    repo.bump_wake(pool, "default")
    client = TestClient(create_app(pool, settings))

    resp = client.get(f"/agents/{a.id}/pause", params={"project": "default"})

    assert resp.status_code == 200
    assert resp.json()["wake_at"] is not None


def test_pause_endpoint_wake_at_matches_write_key_when_project_is_not_a_registry_key(settings, pool):
    """Regression (Opus gate follow-up, HIGH): the write side (POST
    /agents/wake, and the "Wake all" button) and the read side (GET
    /agents/{id}/pause) must resolve `?project=` THE SAME WAY -- both via
    context.current_key(), the registry-resolved key the request middleware
    already derives for every request -- not the raw, unresolved string. With
    an injected pool (as in every dashboard test here) the registry has
    exactly one instance keyed 'default' (see instances.py:_single), so
    registry.resolve_key() folds ANY raw ?project= value that isn't literally
    'default' back onto 'default' -- e.g. a side-car's own --project string,
    which will essentially never equal the literal word 'default'. Before the
    fix, agent_pause_state read get_wake_at(pool, <raw param>) directly, so a
    bump made under a resolved key ('default') and a read made under the RAW,
    unresolved value silently diverged -- the side-car would poll forever and
    never observe the wake. Assert here that a bump posted with a
    NOT-a-registry-key ?project= value, read back with a DIFFERENT
    not-a-registry-key ?project= value, still round-trips -- because both
    resolve to the same registry key, not because the strings happen to
    match."""
    a = repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.post("/agents/wake", params={"project": "not-a-registry-key"},
                       follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get(f"/agents/{a.id}/pause",
                      params={"project": "some-other-unregistered-string"})
    assert resp.status_code == 200
    assert resp.json()["wake_at"] is not None


def test_heartbeat_endpoint_accepts_valid_status(settings, pool):
    a = repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.post(f"/agents/{a.id}/heartbeat", params={"status": "dormant"})

    assert resp.status_code == 200
    assert repo.get_agent(pool, a.id).status == "dormant"


def test_heartbeat_endpoint_never_4xxs_on_invalid_status(settings, pool):
    a = repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.post(f"/agents/{a.id}/heartbeat", params={"status": "bogus"})

    assert resp.status_code == 200
    assert repo.get_agent(pool, a.id).last_seen is not None
    assert repo.get_agent(pool, a.id).status == "idle"   # unaffected, revive-only path


def test_heartbeat_endpoint_with_no_status_unchanged(settings, pool):
    a = repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.post(f"/agents/{a.id}/heartbeat")

    assert resp.status_code == 200
    assert resp.json() == {"agent_id": a.id, "ok": True}


def test_wake_endpoint_bumps_signal_and_redirects(settings, pool):
    client = TestClient(create_app(pool, settings))
    before = repo.get_wake_at(pool, "default")

    resp = client.post("/agents/wake", params={"project": "default"}, follow_redirects=False)

    assert resp.status_code == 303
    after = repo.get_wake_at(pool, "default")
    assert after is not None
    if before is not None:
        assert after >= before


def test_agents_page_renders_wake_all_button(settings, pool):
    repo.register_agent(pool, "backend", "dev")
    client = TestClient(create_app(pool, settings))

    resp = client.get("/agents")

    assert resp.status_code == 200
    assert "/agents/wake?project=" in resp.text
    assert "Wake all" in resp.text
