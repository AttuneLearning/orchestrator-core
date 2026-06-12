"""Integration tests for runtime pipelines and CLI agent worker (slices I + J).

Tests 6-12: hotfix/research/unknown pipelines, new team aliases,
CLI runtime worker, missing cli_agent_cmd error, CLI add-goal validation.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from typing import Any, Optional

import pytest

from orchestrator import repository as repo
from orchestrator.agents.base import GateReview, IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine
from orchestrator.models import Goal, Issue
from orchestrator.roster import load_roster


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_engine(settings, pool, reasoner=None, **threshold_overrides):
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=reasoner or StubReasoner())


def _event_types(pool, issue_id: int) -> list[str]:
    return [e.event_type for e in repo.recent_events(pool, issue_id, limit=200)]


def _events(pool, issue_id: int):
    return repo.recent_events(pool, issue_id, limit=200)


# --------------------------------------------------------------------------- #
# 6. test_hotfix_pipeline_two_gates
# --------------------------------------------------------------------------- #

def test_hotfix_pipeline_two_gates(settings, pool):
    """Issues on the hotfix pipeline pass exactly 2 gates (implementation + qa_gate)."""
    goal = repo.create_goal(pool, "Urgent hotfix", "critical bug", pipeline="hotfix")
    repo.set_goal_state(pool, goal.id, "active")
    repo.create_issue(pool, goal.id, "Fix null pointer", pipeline="hotfix", team="backend")

    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    for issue in issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} state={refreshed.state!r}, expected 'done'"
        )
        assert refreshed.pipeline == "hotfix", (
            f"issue {issue.id} pipeline={refreshed.pipeline!r}, expected 'hotfix'"
        )
        gate_passes = _event_types(pool, issue.id).count("gate_pass")
        assert gate_passes == 2, (
            f"issue {issue.id} hotfix: expected 2 gate_pass, got {gate_passes}"
        )


# --------------------------------------------------------------------------- #
# 7. test_research_pipeline_two_gates
# --------------------------------------------------------------------------- #

def test_research_pipeline_two_gates(settings, pool):
    """Issues on the research pipeline pass exactly 2 gates (intake + completion).
    No code_generated events because there is no implementation gate.
    """
    goal = repo.create_goal(pool, "Research spike", "investigate options", pipeline="research")
    repo.set_goal_state(pool, goal.id, "active")
    repo.create_issue(pool, goal.id, "Spike on auth options", pipeline="research", team="backend")

    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    for issue in issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} state={refreshed.state!r}, expected 'done'"
        )
        etypes = _event_types(pool, issue.id)
        gate_passes = etypes.count("gate_pass")
        assert gate_passes == 2, (
            f"issue {issue.id} research: expected 2 gate_pass, got {gate_passes}"
        )
        assert "code_generated" not in etypes, (
            f"issue {issue.id} research pipeline should not have code_generated events"
        )


# --------------------------------------------------------------------------- #
# 8. test_unknown_goal_pipeline_falls_back
# --------------------------------------------------------------------------- #

def test_unknown_goal_pipeline_falls_back(settings, pool):
    """A goal with an unknown pipeline name causes issues to be created with the
    default pipeline ('pipeline-1') and they complete with 4 gate_pass events."""
    # Create goal directly via repo with an unknown pipeline
    goal = repo.create_goal(pool, "Mystery goal", "unknown pipeline", pipeline="nope")

    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) > 0, "engine should have created issues for the goal"

    for issue in issues:
        refreshed = repo.get_issue(pool, issue.id)
        # Engine falls back to default_pipeline when goal.pipeline is unknown
        assert refreshed.pipeline == "pipeline-1", (
            f"issue {issue.id} pipeline={refreshed.pipeline!r}, expected 'pipeline-1'"
        )
        assert refreshed.state == "done", (
            f"issue {issue.id} state={refreshed.state!r}, expected 'done'"
        )
        gate_passes = _event_types(pool, issue.id).count("gate_pass")
        assert gate_passes == 4, (
            f"issue {issue.id} pipeline-1: expected 4 gate_pass, got {gate_passes}"
        )


# --------------------------------------------------------------------------- #
# 9. test_new_teams_resolvable
# --------------------------------------------------------------------------- #

def test_new_teams_resolvable(settings):
    """Roster aliases: qa/quality both resolve to 'qa'; plat/platform to 'platform'."""
    roster = load_roster(settings.roster)

    qa_team = roster.resolve("qa")
    assert qa_team is not None, "roster.resolve('qa') returned None"
    assert qa_team.id == "qa", f"resolve('qa').id={qa_team.id!r}"

    quality_team = roster.resolve("quality")
    assert quality_team is not None, "roster.resolve('quality') returned None"
    assert quality_team.id == "qa", f"resolve('quality').id={quality_team.id!r}"

    plat_team = roster.resolve("plat")
    assert plat_team is not None, "roster.resolve('plat') returned None"
    assert plat_team.id == "platform", f"resolve('plat').id={plat_team.id!r}"

    platform_team = roster.resolve("platform")
    assert platform_team is not None, "roster.resolve('platform') returned None"
    assert platform_team.id == "platform", f"resolve('platform').id={platform_team.id!r}"


# --------------------------------------------------------------------------- #
# 10. test_cli_runtime_worker
# --------------------------------------------------------------------------- #

def test_cli_runtime_worker(settings, pool):
    """runtime=cli agent uses CliSessionWorker; produces code_generated with provider='cli'
    and a session_started event; agent.last_seen is updated."""
    s = copy.deepcopy(settings)
    s.cli_agent_cmd = "echo CODE for {session_id}"

    goal = repo.create_goal(pool, "CLI goal", "use cli worker")
    repo.register_agent(pool, "backend", "dev", runtime="cli")
    repo.register_agent(pool, "backend", "qa", runtime="api")

    engine = Engine(s, pool, reasoner=StubReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) > 0, "no issues created"

    for issue in issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} state={refreshed.state!r}, expected 'done'"
        )

    # Find issues that went through the implementation gate
    impl_issues = [
        issue for issue in issues
        if "code_generated" in _event_types(pool, issue.id)
    ]
    assert len(impl_issues) > 0, "no issues passed through the implementation gate"

    for issue in impl_issues:
        all_events = _events(pool, issue.id)

        # code_generated event with provider == "cli" and content containing "CODE for issue-"
        code_gen_events = [e for e in all_events if e.event_type == "code_generated"]
        assert len(code_gen_events) >= 1, f"issue {issue.id}: no code_generated events"
        for ev in code_gen_events:
            assert ev.payload.get("provider") == "cli", (
                f"issue {issue.id}: code_generated provider={ev.payload.get('provider')!r}"
            )
            content = ev.payload.get("content", "")
            assert "CODE for issue-" in content, (
                f"issue {issue.id}: code_generated content {content!r} does not contain "
                f"'CODE for issue-'"
            )

        # session_started event exists
        session_events = [e for e in all_events if e.event_type == "session_started"]
        assert len(session_events) >= 1, (
            f"issue {issue.id}: no session_started event found"
        )

    # agent.last_seen is not None for the cli dev agent
    agents = repo.list_agents(pool, "backend")
    cli_agents = [a for a in agents if a.runtime == "cli" and a.function == "dev"]
    assert len(cli_agents) >= 1, "no cli dev agent found"
    for agent in cli_agents:
        refreshed_agent = repo.get_agent(pool, agent.id)
        assert refreshed_agent.last_seen is not None, (
            f"cli agent {agent.id} last_seen is None (touch_agent not called)"
        )


# --------------------------------------------------------------------------- #
# 11. test_cli_runtime_missing_cmd_fails_issue
# --------------------------------------------------------------------------- #

def test_cli_runtime_missing_cmd_fails_issue(settings, pool):
    """When cli_agent_cmd is empty, the work step raises RuntimeError which is
    caught by _advance and logged as an 'error' event; the issue does NOT advance
    to done."""
    s = copy.deepcopy(settings)
    s.cli_agent_cmd = ""
    s.thresholds = copy.deepcopy(settings.thresholds)
    s.thresholds.retry_cap = 1

    goal = repo.create_goal(pool, "CLI missing cmd", "no command configured")
    # Only register cli dev + api qa
    repo.register_agent(pool, "backend", "dev", runtime="cli")
    repo.register_agent(pool, "backend", "qa", runtime="api")

    engine = Engine(s, pool, reasoner=StubReasoner())
    # Run a limited number of ticks so we see the error before retry_cap kills it
    history = engine.run(max_ticks=20)

    all_issues = repo.list_issues(pool, goal_id=goal.id)
    # Find issues that hit the implementation gate (they have the cli dev agent)
    # The error event should mention CLI_AGENT_CMD
    issues_with_error = []
    for issue in all_issues:
        all_events = _events(pool, issue.id)
        error_events = [
            e for e in all_events
            if e.event_type == "error"
            and "CLI_AGENT_CMD" in str(e.payload.get("error", ""))
        ]
        if error_events:
            issues_with_error.append(issue)

    assert len(issues_with_error) >= 1, (
        f"expected at least one issue with CLI_AGENT_CMD error; "
        f"all event types: "
        f"{[(i.id, _event_types(pool, i.id)) for i in all_issues]}"
    )

    # The affected issue should not be 'done'
    for issue in issues_with_error:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state != "done", (
            f"issue {issue.id} should not be 'done' when CLI_AGENT_CMD is missing; "
            f"state={refreshed.state!r}"
        )


# --------------------------------------------------------------------------- #
# 12. test_add_goal_cli_rejects_unknown_pipeline
# --------------------------------------------------------------------------- #

def test_add_goal_cli_rejects_unknown_pipeline(settings, pool):
    """orchestrator CLI returns exit code 1 for an unknown pipeline; no goal created."""
    venv_python = sys.executable

    result = subprocess.run(
        [venv_python, "-m", "orchestrator.cli", "add-goal", "X", "--pipeline", "bogus"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, (
        f"expected returncode 1 for unknown pipeline; got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )

    # No goal should have been created
    goals = repo.list_all_goals(pool)
    assert len(goals) == 0, (
        f"expected no goals after rejected add-goal; got {len(goals)}: "
        f"{[g.title for g in goals]}"
    )
