"""Coordinator-only root issue creation through the MCP surface."""

from __future__ import annotations

import pytest

from orchestrator import repository as repo
from orchestrator.config import load_settings
from orchestrator.mcp_server import tools_issues


class _Recorder:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def _tools(pool, actor_role: str):
    recorder = _Recorder()
    settings = load_settings()
    settings.promote_repo_path = ""
    settings.apply_repo_path = ""
    tools_issues.register(recorder, pool, settings, actor_role=actor_role)
    return recorder.tools


def test_orch_manager_can_create_root_issue_and_goal_child(pool):
    tools = _tools(pool, "orch-manager")
    goal = repo.create_goal(pool, "coordinator goal", pipeline="custom-coordination")

    root = tools["create_issue"](goal.id, "Root issue", "root description")
    assert root["goal_id"] == goal.id
    assert root["parent_id"] is None
    assert root["depth"] == 0
    assert root["pipeline"] == "custom-coordination"

    child = tools["create_goal_child"](goal.id, "Explicit goal child", "child description")
    assert child["goal_id"] == goal.id
    assert child["parent_id"] is None
    assert child["depth"] == 0


@pytest.mark.parametrize("role", ["", "backend-dev-worker", "senior-dev", "orch-manager-2"])
def test_non_orch_manager_cannot_create_root_issue(pool, role):
    tools = _tools(pool, role)
    goal = repo.create_goal(pool, "coordinator goal", pipeline="pull-1")

    with pytest.raises(PermissionError, match="only the orch-manager"):
        tools["create_issue"](goal.id, "Root issue", "root description")
    with pytest.raises(PermissionError, match="only the orch-manager"):
        tools["create_goal_child"](goal.id, "Goal child", "child description")
