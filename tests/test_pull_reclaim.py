"""Liveness reclaim for pull gates: a stale external worker is reclaimed and the
issue is re-routed to a fresh worker. Reclaim-cap breaches alert but do not
quarantine work, because stale CLI workers can be false positives during long
runs.
"""

from __future__ import annotations

import copy

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine


def _engine(settings, pool, **threshold_overrides):
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=StubReasoner())


def _backdate(pool, agent_id, *, seconds=3600):
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE agents SET last_seen = now() - interval '{int(seconds)} seconds' "
            "WHERE id = %s",
            (agent_id,),
        )


def _pull_issue(pool):
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "i", pipeline="pull-1", team="backend")
    repo.register_agent(pool, "backend", "lead", "api")
    return goal, issue


def test_stale_worker_reclaimed_and_reassigned_to_fresh_worker(settings, pool):
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    dev2 = repo.register_agent(pool, "backend", "dev", "external")
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=5)

    eng.run(max_ticks=20)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    # dev1 goes silent (no heartbeat) past the stale window
    _backdate(pool, dev1.id, seconds=3600)
    eng.run(max_ticks=20)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.gate_type == "implementation"           # same gate
    assert refreshed.assigned_agent == dev2.id               # handed to a fresh worker
    assert repo.get_agent(pool, dev1.id).status == "offline"  # dead worker excluded
    assert "reclaimed" in [e.event_type
                           for e in repo.recent_events(pool, issue.id, limit=200)]


def test_stale_worker_without_replacement_is_held_not_nulled(settings, pool):
    # A stale worker with NO live replacement must NOT be nulled: a null-owner
    # issue is invisible to every worker's my_queue poll and would deadlock. The
    # issue stays assigned to its (stale) worker so it resurfaces the moment that
    # worker's process polls again; a one-shot worker_stale_hold event gives
    # visibility, and the goal is never paused.
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=1)

    eng.run(max_ticks=20)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    _backdate(pool, dev1.id, seconds=3600)
    eng.run(max_ticks=20)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.state == "in_progress"
    assert refreshed.assigned_agent == dev1.id                # HELD, not nulled
    assert repo.get_agent(pool, dev1.id).status == "offline"  # flagged for visibility
    paused_ids = [g.id for g in repo.list_goals_by_state(pool, "paused")]
    assert goal.id not in paused_ids
    events = repo.recent_events(pool, issue.id, limit=200)
    holds = [e for e in events if e.event_type == "worker_stale_hold"]
    assert len(holds) == 1                                    # one-shot, not per-tick
    # never nulled → never reclaimed, so no reclaim_cap alert on the hold path
    assert not [e for e in events if e.event_type == "reclaimed"]


def test_held_issue_resumes_when_worker_polls(settings, pool):
    # After a hold, the worker's heartbeat/poll revives it (offline -> idle) and
    # the held issue is immediately actionable again — no reassignment needed.
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=1)
    eng.run(max_ticks=20)
    _backdate(pool, dev1.id, seconds=3600)
    eng.run(max_ticks=20)
    assert repo.get_agent(pool, dev1.id).status == "offline"
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    repo.touch_agent(pool, dev1.id)                           # worker polls again
    assert repo.get_agent(pool, dev1.id).status == "idle"     # revived
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id  # still its work
