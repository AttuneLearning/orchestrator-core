"""Tests for the status/alert/suggestion plugin surface (test_mcp_status.py).

Covers the shared monitoring rollups, the cross-issue event cursor, the gated
goal-proposal lifecycle, and the MCP tool bodies in mcp_server/tools_status.py.
Requires Postgres (conftest pool fixture + per-test truncate).
"""

from __future__ import annotations

import copy

import pytest

from orchestrator import monitoring
from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine
from orchestrator.mcp_server import tools_status


class _Recorder:
    """Captures @mcp.tool()-decorated functions so we can call them directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(pool, settings):
    rec = _Recorder()
    tools_status.register(rec, pool, settings)
    return rec.tools


# --------------------------------------------------------------------------- #
# events_since cursor
# --------------------------------------------------------------------------- #

def test_events_since_orders_and_pages(settings, pool):
    goal = repo.create_goal(pool, "G")
    a = repo.create_issue(pool, goal.id, "A")
    b = repo.create_issue(pool, goal.id, "B")  # each create appends a 'created' event

    all_events = repo.events_since(pool, after_id=0, limit=100)
    ids = [e.id for e in all_events]
    assert ids == sorted(ids), "feed is oldest-first by global id"
    assert {e.issue_id for e in all_events} == {a.id, b.id}, "feed spans issues"

    # advancing the cursor returns only newer rows
    cursor = all_events[0].id
    rest = repo.events_since(pool, after_id=cursor, limit=100)
    assert all(e.id > cursor for e in rest)
    assert len(rest) == len(all_events) - 1


# --------------------------------------------------------------------------- #
# gated proposal lifecycle
# --------------------------------------------------------------------------- #

def test_propose_goal_is_suggested_and_inert(settings, pool):
    goal = repo.propose_goal(pool, "Idea", "from a looping agent",
                             suggested_by="hermes", source="noticed a gap")
    assert goal.state == "suggested"
    assert goal.suggested_by == "hermes"

    # not an "open" goal (hidden from the engine's open-goal views)
    assert goal.id not in {g.id for g in repo.list_open_goals(pool)}
    assert goal.id in {g.id for g in repo.list_goals_by_state(pool, "suggested")}

    # the engine must not touch a suggested goal
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    Engine(copy.deepcopy(settings), pool, reasoner=StubReasoner()).run()
    after = next(g for g in repo.list_all_goals(pool) if g.id == goal.id)
    assert after.state == "suggested", "suggested goal stayed inert"
    assert repo.list_issues(pool, goal_id=goal.id) == [], "no issues created"


def test_promote_goal_lets_engine_run_it(settings, pool):
    goal = repo.propose_goal(pool, "Build a thing")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    repo.promote_goal(pool, goal.id)
    promoted = next(g for g in repo.list_all_goals(pool) if g.id == goal.id)
    assert promoted.state == "backlog"

    Engine(copy.deepcopy(settings), pool, reasoner=StubReasoner()).run()
    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 1 and all(i.state == "done" for i in issues)


def test_reject_goal(settings, pool):
    goal = repo.propose_goal(pool, "Bad idea")
    repo.reject_goal(pool, goal.id)
    assert next(g for g in repo.list_all_goals(pool)
                if g.id == goal.id).state == "rejected"


def test_promote_reject_require_suggested_state(settings, pool):
    goal = repo.create_goal(pool, "Normal backlog goal")  # state 'backlog'
    with pytest.raises(ValueError):
        repo.promote_goal(pool, goal.id)
    with pytest.raises(ValueError):
        repo.reject_goal(pool, goal.id)


# --------------------------------------------------------------------------- #
# MCP tool bodies
# --------------------------------------------------------------------------- #

def test_get_status_tool_matches_monitoring(settings, pool):
    repo.create_goal(pool, "Visible goal")
    repo.propose_goal(pool, "Suggested goal", suggested_by="agent")
    tools = _tools(pool, settings)

    status = tools["get_status"]()
    expected = monitoring.fleet_summary(pool, settings)
    assert status["goals"] == expected["goals"]
    assert status["fleet_focus"] == expected["fleet_focus"]
    assert "agents" in status
    assert [g["title"] for g in status["suggested_goals"]] == ["Suggested goal"]


def test_get_alerts_tool_surfaces_attention_set(settings, pool):
    tools = _tools(pool, settings)
    alerts = tools["get_alerts"]()
    # blank slate: nothing wrong
    assert alerts["below_threshold"] is False
    assert alerts["flagged_issues"] == []
    assert alerts["paused_goals"] == []
    assert alerts["stale_agents"] == []
    assert set(alerts) == {"below_threshold", "fleet_focus", "flagged_issues",
                           "paused_goals", "stale_agents"}


def test_tail_events_and_propose_goal_tools(settings, pool):
    tools = _tools(pool, settings)

    created = tools["propose_goal"](title="Idea", suggested_by="openclaw")
    assert created["state"] == "suggested" and created["suggested_by"] == "openclaw"

    goal = repo.create_goal(pool, "G")
    repo.create_issue(pool, goal.id, "A")
    events = tools["tail_events"](after_id=0, limit=100)
    assert events and all("id" in e and "event_type" in e for e in events)
    # cursor advances
    top = max(e["id"] for e in events)
    assert tools["tail_events"](after_id=top) == []
