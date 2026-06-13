"""Liveness reclaim for pull gates: a stale external worker is reclaimed; the
issue is re-routed to a fresh worker, or quarantined after reclaim_cap reclaims.
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


def test_reclaim_cap_quarantines_to_off_rails(settings, pool):
    goal, issue = _pull_issue(pool)
    dev1 = repo.register_agent(pool, "backend", "dev", "external")
    eng = _engine(settings, pool, agent_stale_seconds=60, reclaim_cap=1)

    eng.run(max_ticks=20)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev1.id

    _backdate(pool, dev1.id, seconds=3600)
    eng.run(max_ticks=20)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.state == "off_rails"                    # quarantined at the cap
    paused_ids = [g.id for g in repo.list_goals_by_state(pool, "paused")]
    assert goal.id in paused_ids
