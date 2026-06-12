"""Integration tests for the FastAPI ops dashboard (test_dashboard.py).

Requires a running Postgres instance with migrations applied.  The _clean_db
autouse fixture in conftest.py truncates all mutable tables before each test,
so every test starts from a blank slate.
"""

from __future__ import annotations

import copy
import warnings
from typing import Any, Optional

warnings.filterwarnings("ignore")

import pytest
from fastapi.testclient import TestClient

from orchestrator import repository as repo
from orchestrator.agents.base import GateReview, IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.dashboard.app import create_app
from orchestrator.engine.loop import Engine
from orchestrator.models import Goal, Issue


# --------------------------------------------------------------------------- #
# Custom reasoners
# --------------------------------------------------------------------------- #

class _LowDriftDecliningReasoner:
    """Always declines gate reviews with drift 0.1 so off_rails latch fires."""

    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]:
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
        return 0.1


def _off_rails_settings(settings):
    """Return a deepcopy of settings with retry_cap=10 for off-rails scenarios."""
    s = copy.deepcopy(settings)
    s.thresholds.retry_cap = 10
    return s


def _run_off_rails(settings, pool):
    """Set up and run an off-rails scenario; returns (goal, off_rails_settings)."""
    s = _off_rails_settings(settings)
    goal = repo.create_goal(pool, "Off-rails goal", "goes off the rails")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    engine = Engine(s, pool, reasoner=_LowDriftDecliningReasoner())
    engine.run()
    return goal, s


# --------------------------------------------------------------------------- #
# 1. test_empty_overview
# --------------------------------------------------------------------------- #

def test_empty_overview(settings, pool):
    client = TestClient(create_app(pool, settings))

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Fleet overview" in resp.text

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["fleet_focus"] == pytest.approx(1.0)
    assert data["flagged"] == 0
    assert data["below_threshold"] is False


# --------------------------------------------------------------------------- #
# 2. test_happy_overview
# --------------------------------------------------------------------------- #

def test_happy_overview(settings, pool):
    goal = repo.create_goal(pool, "Happy goal", "do the thing")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = Engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    client = TestClient(create_app(pool, settings))

    resp = client.get("/")
    assert resp.status_code == 200

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["issues"].get("done", 0) == 2
    assert data["fleet_focus"] == pytest.approx(1.0)
    assert data["flagged"] == 0
    assert data["below_threshold"] is False

    goals_list = data["goals_list"]
    assert len(goals_list) == 1
    assert goals_list[0]["issue_count"] == 2


# --------------------------------------------------------------------------- #
# 3. test_goal_and_issue_pages
# --------------------------------------------------------------------------- #

def test_goal_and_issue_pages(settings, pool):
    goal = repo.create_goal(pool, "Test goal title", "goal description")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = Engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    # Determine ids from the repo instead of hardcoding
    goals = repo.list_all_goals(pool)
    assert len(goals) == 1
    goal_id = goals[0].id

    issues = repo.list_issues(pool, goal_id=goal_id)
    assert len(issues) >= 1
    issue_id = issues[0].id

    client = TestClient(create_app(pool, settings))

    resp = client.get(f"/goals/{goal_id}")
    assert resp.status_code == 200
    assert "Test goal title" in resp.text

    resp = client.get(f"/issues/{issue_id}")
    assert resp.status_code == 200
    assert "Timeline" in resp.text

    resp = client.get("/goals/9999")
    assert resp.status_code == 404

    resp = client.get("/issues/9999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 4. test_agents_page
# --------------------------------------------------------------------------- #

def test_agents_page(settings, pool):
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "qa", "tester")

    client = TestClient(create_app(pool, settings))

    resp = client.get("/agents")
    assert resp.status_code == 200
    assert "backend" in resp.text
    assert "qa" in resp.text


# --------------------------------------------------------------------------- #
# 5. test_off_rails_surfaces_in_dashboard
# --------------------------------------------------------------------------- #

def test_off_rails_surfaces_in_dashboard(settings, pool):
    goal, s = _run_off_rails(settings, pool)

    client = TestClient(create_app(pool, s))

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["flagged"] >= 1
    assert data["fleet_focus"] < 1.0
    assert data["below_threshold"] is True
    assert len(data["paused_goals"]) >= 1

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Fleet focus" in resp.text
    assert "Paused goals" in resp.text


# --------------------------------------------------------------------------- #
# 6. test_directive_route_unquarantines
# --------------------------------------------------------------------------- #

def test_directive_route_unquarantines(settings, pool):
    goal, s = _run_off_rails(settings, pool)

    # Find an off_rails issue
    issues = repo.list_issues(pool)
    off_rails_issues = [i for i in issues if i.state == "off_rails"]
    assert len(off_rails_issues) >= 1, "precondition: at least one off_rails issue"
    issue = off_rails_issues[0]

    client = TestClient(create_app(pool, s))

    resp = client.post(f"/issues/{issue.id}/directive", follow_redirects=False)
    assert resp.status_code == 303

    # Verify state after directive
    updated = repo.get_issue(pool, issue.id)
    assert updated.state == "in_progress"
    assert updated.retry_count == 0
    assert updated.step_count == 0

    # Verify directive event in timeline
    timeline = repo.issue_timeline(pool, issue.id)
    directive_events = [e for e in timeline if e.event_type == "directive"]
    assert len(directive_events) == 1


# --------------------------------------------------------------------------- #
# 7. test_resume_goal_route
# --------------------------------------------------------------------------- #

def test_resume_goal_route(settings, pool):
    goal, s = _run_off_rails(settings, pool)

    # Verify goal is paused
    goals = repo.list_all_goals(pool)
    paused = [g for g in goals if g.state == "paused"]
    assert len(paused) >= 1, "precondition: at least one paused goal"
    paused_goal = paused[0]

    client = TestClient(create_app(pool, s))

    resp = client.post(f"/goals/{paused_goal.id}/resume", follow_redirects=False)
    assert resp.status_code == 303

    # Verify goal is now active
    all_goals = repo.list_all_goals(pool)
    resumed = next((g for g in all_goals if g.id == paused_goal.id), None)
    assert resumed is not None
    assert resumed.state == "active"


# --------------------------------------------------------------------------- #
# 8. test_full_recovery_cycle
# --------------------------------------------------------------------------- #

def test_full_recovery_cycle(settings, pool):
    goal, s = _run_off_rails(settings, pool)

    # Find all off_rails issues and the paused goal
    all_issues = repo.list_issues(pool)
    off_rails_issues = [i for i in all_issues if i.state == "off_rails"]
    assert len(off_rails_issues) >= 1, "precondition: at least one off_rails issue"

    all_goals = repo.list_all_goals(pool)
    paused_goals = [g for g in all_goals if g.state == "paused"]
    assert len(paused_goals) >= 1, "precondition: at least one paused goal"

    client = TestClient(create_app(pool, s))

    # POST directive for each off_rails issue
    for issue in off_rails_issues:
        resp = client.post(f"/issues/{issue.id}/directive", follow_redirects=False)
        assert resp.status_code == 303

    # POST resume for each paused goal
    for pg in paused_goals:
        resp = client.post(f"/goals/{pg.id}/resume", follow_redirects=False)
        assert resp.status_code == 303

    # Run a fresh engine with StubReasoner to completion
    engine2 = Engine(s, pool, reasoner=StubReasoner())
    engine2.run()

    # Verify dashboard shows full recovery
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["fleet_focus"] == pytest.approx(1.0)
    assert data["flagged"] == 0

    # Verify the previously off_rails issues are now done
    for issue in off_rails_issues:
        updated = repo.get_issue(pool, issue.id)
        assert updated.state == "done", \
            f"issue {updated.id} expected 'done', got {updated.state!r}"
