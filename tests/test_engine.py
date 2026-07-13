"""Integration tests for the Engine tick loop (test_engine.py).

Requires a running Postgres instance (DATABASE_URL / localhost default) with
migrations applied.  The _clean_db autouse fixture in conftest.py truncates
all mutable tables before each test, so every test starts from a blank slate.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from orchestrator import repository as repo
from orchestrator.agents.base import GateReview, IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.models import Goal, Issue


# --------------------------------------------------------------------------- #
# Custom reasoners used by multiple tests
# --------------------------------------------------------------------------- #

class _DecliningReasoner:
    """Always declines gate reviews; drift 1.0 (so off_rails latch never fires)."""

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "") -> list[IssueSpec]:
        return [
            IssueSpec(title=f"Implement: {goal.title}", description=goal.description),
            IssueSpec(title=f"Test: {goal.title}", description="Add tests and verify."),
        ][:max_subissues]

    def plan_issue(self, issue: Issue) -> str:
        return f"Plan for '{issue.title}': implement, add tests, verify, complete."

    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list[dict[str, Any]]] = None) -> GateReview:
        return GateReview(passed=False, reasons=["nope"])

    def score_drift(self, issue: Issue,
                    recent: Optional[list[dict[str, Any]]] = None) -> float:
        return 1.0


class _LowDriftDecliningReasoner(_DecliningReasoner):
    """Always declines; drift 0.1 so off_rails latch fires once oscillation fires."""

    def score_drift(self, issue: Issue,
                    recent: Optional[list[dict[str, Any]]] = None) -> float:
        return 0.1


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_engine(settings, pool, reasoner=None, **threshold_overrides):
    """Build an Engine with an independent deep-copy of settings."""
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=reasoner or StubReasoner())


def _event_types(pool, issue_id: int) -> list[str]:
    return [e.event_type for e in repo.recent_events(pool, issue_id, limit=200)]


# --------------------------------------------------------------------------- #
# 1. Happy path
# --------------------------------------------------------------------------- #

def test_happy_path(settings, pool):
    goal = repo.create_goal(pool, "Happy goal", "do the thing")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 1, "StubReasoner decomposes into one implementation issue"
    for issue in issues:
        assert issue.state == "done", f"issue {issue.id} state={issue.state!r}"

    # goal reached done
    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "done"

    # agents back to idle
    agents = repo.list_agents(pool, "backend")
    assert all(a.status == "idle" for a in agents), \
        f"agents not idle: {[a.status for a in agents]}"

    # each issue has exactly one "plan" event and zero "drift_score" events
    for issue in issues:
        etypes = _event_types(pool, issue.id)
        assert etypes.count("plan") == 1, f"issue {issue.id}: plan count={etypes.count('plan')}"
        assert etypes.count("drift_score") == 0, \
            f"issue {issue.id}: unexpected drift_score events"


# --------------------------------------------------------------------------- #
# 2. on_tick callback
# --------------------------------------------------------------------------- #

def test_on_tick_callback(settings, pool):
    repo.create_goal(pool, "Callback goal", "test callback")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool)
    collected: list[TickSummary] = []
    history = engine.run(on_tick=collected.append)

    assert len(collected) == len(history), \
        f"on_tick called {len(collected)}x but history has {len(history)} entries"


# --------------------------------------------------------------------------- #
# 3. Failure path (retry cap hit)
# --------------------------------------------------------------------------- #

def test_failure_path(settings, pool):
    goal = repo.create_goal(pool, "Failing goal", "always fails")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=_DecliningReasoner(), retry_cap=2)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    failed = [i for i in issues if i.state == "failed"]
    assert len(failed) >= 1, "at least one issue should be failed"
    assert any(i.retry_count == 2 for i in failed), \
        f"expected retry_count==2 on a failed issue; got {[i.retry_count for i in failed]}"

    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "done", f"goal state={row[0]!r}, expected 'done'"
    pending = repo.pending_messages(pool, to_team="orch-monitor")
    assert pending and "unresolved issue" in pending[0]["subject"]


# --------------------------------------------------------------------------- #
# 4. Off-rails quarantine (oscillation + low drift)
# --------------------------------------------------------------------------- #

def test_off_rails_quarantine(settings, pool):
    """3+ gate_decline events fire oscillation signal; low drift latches off_rails."""
    goal = repo.create_goal(pool, "Off-rails goal", "goes off the rails")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    # retry_cap=10 ensures FAILED is never reached before oscillation fires
    engine = _make_engine(settings, pool, reasoner=_LowDriftDecliningReasoner(), retry_cap=10)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    off_rails = [i for i in issues if i.state == "off_rails"]
    assert len(off_rails) >= 1, \
        f"expected at least one off_rails issue; states={[i.state for i in issues]}"

    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "paused", f"goal state={row[0]!r}, expected 'paused'"

    # at least one off_rails issue has a drift_score event with drift 0.1
    for issue in off_rails:
        etypes = _event_types(pool, issue.id)
        assert "drift_score" in etypes, \
            f"issue {issue.id} off_rails but no drift_score event"
        # find the actual drift value in events
        for ev in repo.recent_events(pool, issue.id, limit=200):
            if ev.event_type == "drift_score":
                assert ev.payload.get("drift") == pytest.approx(0.1), \
                    f"drift_score payload drift={ev.payload.get('drift')!r}"
                break


# --------------------------------------------------------------------------- #
# 5. Directive un-quarantine
# --------------------------------------------------------------------------- #

def test_directive_unquarantine(settings, pool):
    """After off_rails quarantine, apply_directive resets counters and work resumes."""
    goal = repo.create_goal(pool, "Directive goal", "quarantine then resume")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    # Phase 1: trigger off_rails
    engine = _make_engine(settings, pool, reasoner=_LowDriftDecliningReasoner(), retry_cap=10)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    off_rails_issues = [i for i in issues if i.state == "off_rails"]
    assert len(off_rails_issues) >= 1, "precondition: at least one off_rails issue"

    # Apply directive to each off_rails issue
    for issue in off_rails_issues:
        restored = repo.apply_directive(pool, issue.id)
        assert restored.state == "in_progress", \
            f"after directive: state={restored.state!r}, expected 'in_progress'"
        assert restored.retry_count == 0, \
            f"after directive: retry_count={restored.retry_count}"
        assert restored.step_count == 0, \
            f"after directive: step_count={restored.step_count}"
        assert restored.gate_type == issue.gate_type, \
            f"gate_type changed from {issue.gate_type!r} to {restored.gate_type!r}"

    # apply_directive reactivates the closed/paused goal automatically.
    assert repo.get_goal(pool, goal.id).state == "active"

    # Phase 2: run with StubReasoner (passes everything)
    # Swap reasoner and create a fresh engine instance on the same pool
    engine2 = _make_engine(settings, pool, reasoner=StubReasoner(), retry_cap=10)
    engine2.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert all(i.state == "done" for i in issues), \
        f"expected all done after directive; states={[i.state for i in issues]}"

    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "done", f"goal state={row[0]!r}, expected 'done'"

    # Each formerly-quarantined issue has exactly one "directive" event
    for issue in off_rails_issues:
        etypes = _event_types(pool, issue.id)
        assert etypes.count("directive") == 1, \
            f"issue {issue.id}: directive count={etypes.count('directive')}"


# --------------------------------------------------------------------------- #
# 6. Directive rejected when not off_rails / goal not paused
# --------------------------------------------------------------------------- #

def test_directive_rejected_when_not_off_rails(settings, pool):
    goal = repo.create_goal(pool, "Normal goal", "no quarantine")
    issue = repo.create_issue(pool, goal.id, "Fresh issue", team="backend")

    # apply_directive on a non-off_rails issue raises ValueError
    with pytest.raises(ValueError):
        repo.apply_directive(pool, issue.id)

    # resume_goal on a non-paused goal raises ValueError
    with pytest.raises(ValueError):
        repo.resume_goal(pool, goal.id)


# --------------------------------------------------------------------------- #
# 7. comms_response gate conditional routing
# --------------------------------------------------------------------------- #

def test_comms_response_gate(settings, pool):
    """triggered_by_message=True issues pass through comms_response; others skip it."""
    goal = repo.create_goal(pool, "Comms goal", "test comms gate")
    repo.set_goal_state(pool, goal.id, "active")

    triggered = repo.create_issue(
        pool, goal.id, "Fix bug", team="backend", triggered_by_message=True
    )
    untriggered = repo.create_issue(
        pool, goal.id, "Fix other", team="backend", triggered_by_message=False
    )

    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    # Both issues must reach done
    t_issue = repo.get_issue(pool, triggered.id)
    u_issue = repo.get_issue(pool, untriggered.id)
    assert t_issue.state == "done", f"triggered issue state={t_issue.state!r}"
    assert u_issue.state == "done", f"untriggered issue state={u_issue.state!r}"

    # Triggered issue passes 5 gates; untriggered passes 4
    t_events = _event_types(pool, triggered.id)
    u_events = _event_types(pool, untriggered.id)

    t_pass_count = t_events.count("gate_pass")
    u_pass_count = u_events.count("gate_pass")

    assert t_pass_count == 5, \
        f"triggered issue: expected 5 gate_pass events, got {t_pass_count}"
    assert u_pass_count == 4, \
        f"untriggered issue: expected 4 gate_pass events, got {u_pass_count}"


# --------------------------------------------------------------------------- #
# 8. Re-engagement (step budget exhaustion)
# --------------------------------------------------------------------------- #

def test_reengagement(settings, pool):
    """With step_budget=2 and 4 work steps (gates), reengagement fires exactly once."""
    goal = repo.create_goal(pool, "Reengagement goal", "test re-engage")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner(), step_budget=2, retry_cap=10)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert all(i.state == "done" for i in issues), \
        f"expected all done; states={[i.state for i in issues]}"

    for issue in issues:
        etypes = _event_types(pool, issue.id)
        assert etypes.count("reengaged") == 1, \
            f"issue {issue.id}: reengaged count={etypes.count('reengaged')}"
        assert etypes.count("context_snapshot") == 1, \
            f"issue {issue.id}: context_snapshot count={etypes.count('context_snapshot')}"


# --------------------------------------------------------------------------- #
# Promote-conflict handling: hold + alert, never silently complete (loop.py)
# --------------------------------------------------------------------------- #

def _at_completion(pool, pipeline="pull-1"):
    goal = repo.create_goal(pool, "promote goal", "x", pipeline=pipeline)
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "i", pipeline=pipeline, team="backend")
    repo.update_state(pool, issue.id, "in_review", gate_type="completion")
    return goal, issue


def test_promote_conflict_holds_issue_not_done(settings, pool, monkeypatch):
    calls = {"n": 0}
    def fake_promote(pool_, issue_, settings_):
        calls["n"] += 1
        return {"promoted": False, "conflict": True,
                "branch": f"issue-{issue_.id}", "target": "main"}
    import orchestrator.apply.worktree as wt
    monkeypatch.setattr(wt, "auto_promote_on_done", fake_promote)

    eng = _make_engine(settings, pool)
    eng.settings.auto_promote_enabled = True
    eng.settings.promote_repo_path = "/tmp/does-not-matter-mocked"
    eng.settings.promote_branch = "main"
    goal, issue = _at_completion(pool)

    eng.tick()
    r = repo.get_issue(pool, issue.id)
    assert r.state == "in_progress"          # HELD, not done
    assert r.gate_type == "completion"
    evs = _event_types(pool, issue.id)
    assert "promoted" not in evs             # nothing falsely promoted
    alerts = [e for e in repo.recent_events(pool, issue.id, limit=200)
              if e.event_type == "alert" and e.payload.get("reason") == "promote_conflict"]
    assert len(alerts) == 1

    # guard: a held issue is NOT re-promoted every tick (no churn, no re-alert)
    eng.tick(); eng.tick()
    assert calls["n"] == 1
    assert repo.get_issue(pool, issue.id).state == "in_progress"


def test_clean_promote_still_completes(settings, pool, monkeypatch):
    import orchestrator.apply.worktree as wt
    monkeypatch.setattr(wt, "auto_promote_on_done",
                        lambda pool_, i_, s_: {"promoted": True, "branch": f"issue-{i_.id}",
                                               "target": "main", "merge_commit": "abc123"})
    eng = _make_engine(settings, pool)
    eng.settings.auto_promote_enabled = True
    eng.settings.promote_repo_path = "/tmp/mocked"
    goal, issue = _at_completion(pool)
    eng.tick()
    assert repo.get_issue(pool, issue.id).state == "done"   # clean promote → completes
