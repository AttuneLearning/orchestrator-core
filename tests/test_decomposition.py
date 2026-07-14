"""Decomposition / routing fixes (spec: engine-decomposition-routing-fixes.md).

Pure tests for orchestrator.decomposition + state-machine cancel, plus engine
integration for pipeline-team inheritance (E1), the decompose override + caps
(E2), the QA-duplicate filter (E3), and the routing-invariant alert (E4).
"""

from __future__ import annotations

import copy

from orchestrator import decomposition as dec
from orchestrator import repository as repo
from orchestrator.agents.base import IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.models import Goal, Issue, IssueState
from orchestrator.state_machine import validate_transition


# --------------------------------------------------------------------------- #
# Pure: simple-goal heuristic + decompose_mode
# --------------------------------------------------------------------------- #

def test_is_simple_goal_dependency_bump():
    assert dec.is_simple_goal("Upgrade Vite 7→8 + plugin-react 5→6")
    assert dec.is_simple_goal("Bump lodash to 4.17.21")
    assert dec.is_simple_goal("Pin the node version")


def test_is_simple_goal_feature_is_not_simple():
    assert not dec.is_simple_goal("Build a new reporting dashboard")
    assert not dec.is_simple_goal("Add a learner enrollment feature")
    # 'update' wording does not make a feature simple
    assert not dec.is_simple_goal("Create a new billing subsystem and update deps")


def test_decompose_mode_override_wins():
    assert dec.decompose_mode("single", "Build a new feature") == dec.SINGLE
    assert dec.decompose_mode("full", "Bump deps") == dec.FULL
    # omitted → heuristic
    assert dec.decompose_mode(None, "Upgrade webpack 4→5") == dec.SINGLE
    assert dec.decompose_mode(None, "Build a new payments service") == dec.FULL


# --------------------------------------------------------------------------- #
# Pure: QA-duplicate filter
# --------------------------------------------------------------------------- #

def test_is_qa_duplicate_drops_gate_work():
    for t in ["Test: the thing", "Run unit tests", "Typecheck the build",
              "Lint everything", "E2E smoke", "HMR testing", "Smoke test login",
              "Bundle output compatibility", "Output Format Compatibility (ESM/CJS/UMD)",
              "Verify the upgrade", "Verification of the API"]:
        assert dec.is_qa_duplicate(t), t


def test_is_qa_duplicate_keeps_real_work():
    for t in ["Implement: the health endpoint", "Add a Spinner component",
              "Refactor the auth middleware", "Wire up the contest form"]:
        assert not dec.is_qa_duplicate(t), t


def test_drop_qa_duplicates_filters_specs():
    specs = [IssueSpec("Implement: X"), IssueSpec("Test: X"),
             IssueSpec("Run unit tests"), IssueSpec("Add a widget")]
    kept = [s.title for s in dec.drop_qa_duplicates(specs)]
    assert kept == ["Implement: X", "Add a widget"]


# --------------------------------------------------------------------------- #
# Pure: routing invariant
# --------------------------------------------------------------------------- #

def test_routing_violations_pure():
    known = {"frontend", "backend"}
    resolve = lambda t: object() if t in known else None
    # all good, matches pipeline team
    assert dec.routing_violations([(1, "frontend")], "frontend", resolve) == []
    # unknown team
    assert dec.routing_violations([(1, "nope")], None, resolve)
    # mismatched pipeline team
    assert dec.routing_violations([(1, "backend")], "frontend", resolve)


# --------------------------------------------------------------------------- #
# Pure: decomposition tiers (bootstrap capability presets)
# --------------------------------------------------------------------------- #

def test_tier_names_are_the_three_levels():
    assert set(dec.tier_names()) == {dec.HIGH, dec.MID, dec.REMEDIAL}
    assert dec.DEFAULT_TIER == dec.MID


def test_resolve_tier_falls_back_to_default_on_unknown():
    assert dec.resolve_tier("high").name == dec.HIGH
    assert dec.resolve_tier("REMEDIAL").name == dec.REMEDIAL   # case-insensitive
    assert dec.resolve_tier("  mid ").name == dec.MID          # trimmed
    assert dec.resolve_tier("nonsense").name == dec.MID        # unknown -> default
    assert dec.resolve_tier(None).name == dec.MID              # empty -> default


def test_tier_behavior_flags_scale_by_capability():
    high, mid, rem = (dec.resolve_tier(t) for t in (dec.HIGH, dec.MID, dec.REMEDIAL))
    # Only the high tier lifts the per-issue internal-parallelism ban.
    assert high.internal_parallelism and not mid.internal_parallelism \
        and not rem.internal_parallelism
    # Only the remedial tier runs mid-run drift checks.
    assert rem.midrun_checks and not high.midrun_checks and not mid.midrun_checks
    # Sizing widens with capability: high spans several files, remedial is one file.
    assert "cohesive" in high.sizing.lower()
    assert "one deliverable" in mid.sizing.lower()
    assert "smallest" in rem.sizing.lower()


# --------------------------------------------------------------------------- #
# Pure: state-machine cancel
# --------------------------------------------------------------------------- #

def test_cancel_transitions():
    C = IssueState.CANCELLED.value
    assert validate_transition(IssueState.IN_PROGRESS.value, C)
    assert validate_transition(IssueState.BACKLOG.value, C)
    assert validate_transition(IssueState.FAILED.value, C)
    assert validate_transition(IssueState.OFF_RAILS.value, C)
    assert not validate_transition(IssueState.DONE.value, C)
    assert not validate_transition(C, IssueState.IN_PROGRESS.value)  # terminal


# --------------------------------------------------------------------------- #
# Reasoners for engine integration
# --------------------------------------------------------------------------- #

class _MisroutingReasoner(StubReasoner):
    """Returns the impl issue tagged with the WRONG team (backend) — the engine
    must override it with the pipeline's team."""

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "", sizing: str = "") -> list[IssueSpec]:
        return [IssueSpec(title=f"Implement: {goal.title}",
                          description=goal.description, team="backend")]


class _NoisyReasoner(StubReasoner):
    """Emits an impl issue plus QA-gate duplicates the filter must drop."""

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "", sizing: str = "") -> list[IssueSpec]:
        return [IssueSpec(title=f"Implement: {goal.title}"),
                IssueSpec(title="Run unit tests"),
                IssueSpec(title="Typecheck"),
                IssueSpec(title="Output Format Compatibility (ESM/CJS/UMD)")]


class _UnknownTeamReasoner(StubReasoner):
    """Pipeline has no team; reasoner emits an unknown team → routing violation."""

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "", sizing: str = "") -> list[IssueSpec]:
        return [IssueSpec(title=f"Implement: {goal.title}", team="nonsense")]


def _engine(settings, pool, reasoner, **overrides):
    s = copy.deepcopy(settings)
    for k, v in overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=reasoner)


# --------------------------------------------------------------------------- #
# E1: pipeline team inheritance
# --------------------------------------------------------------------------- #

def test_pipeline_team_overrides_reasoner(settings, pool):
    goal = repo.create_goal(pool, "Add a learner dashboard", pipeline="pull-fe")
    engine = _engine(settings, pool, _MisroutingReasoner())
    engine.tick()
    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 1
    assert issues[0].team == "frontend", (
        f"child team {issues[0].team!r} should inherit pipeline team 'frontend'")
    assert issues[0].pipeline == "pull-fe"


# --------------------------------------------------------------------------- #
# E2: decompose override
# --------------------------------------------------------------------------- #

def test_decompose_single_override_forces_one_issue(settings, pool):
    # An always-decomposing architect would split, but 'single' suppresses it.
    from orchestrator.agents.base import ComplexityAssessment

    class _Arch(StubReasoner):
        def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
            return ComplexityAssessment(
                decompose=True, subissues=[IssueSpec("Part A"), IssueSpec("Part B")])

    goal = repo.create_goal(pool, "Build a big feature", decompose="single")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    engine = _engine(settings, pool, _Arch())
    engine.run()
    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 1, f"single override must yield 1 issue; got {len(issues)}"


def test_decompose_full_override_allows_split(settings, pool):
    from orchestrator.agents.base import ComplexityAssessment

    class _Arch(StubReasoner):
        def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
            if issue.depth == 0:
                return ComplexityAssessment(
                    decompose=True, subissues=[IssueSpec("Part A"), IssueSpec("Part B")])
            return ComplexityAssessment(decompose=False, subissues=[])

    # 'Upgrade ...' would be simple, but the explicit 'full' override forces a split.
    goal = repo.create_goal(pool, "Upgrade everything", decompose="full")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    engine = _engine(settings, pool, _Arch())
    engine.run()
    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 3, f"full override should split (1+2); got {len(issues)}"


# --------------------------------------------------------------------------- #
# E3: QA-duplicate filter at decomposition
# --------------------------------------------------------------------------- #

def test_qa_duplicates_filtered_at_decompose(settings, pool):
    goal = repo.create_goal(pool, "Add a thing", pipeline="pull-1")
    engine = _engine(settings, pool, _NoisyReasoner())
    engine.tick()
    titles = [i.title for i in repo.list_issues(pool, goal_id=goal.id)]
    assert titles == ["Implement: Add a thing"], (
        f"QA-gate duplicates must be dropped; got {titles}")


# --------------------------------------------------------------------------- #
# E4: routing-invariant alert
# --------------------------------------------------------------------------- #

def test_routing_invariant_holds_goal(settings, pool):
    goal = repo.create_goal(pool, "Do something", pipeline="pipeline-1")
    engine = _engine(settings, pool, _UnknownTeamReasoner())
    summary = engine.tick()
    assert summary.alerts >= 1
    refreshed = repo.get_goal(pool, goal.id)
    assert refreshed.state == "paused", "routing violation must hold (pause) the goal"


# --------------------------------------------------------------------------- #
# E5: cancel_issue + reconcile terminal
# --------------------------------------------------------------------------- #

def test_cancel_issue_releases_agent_and_is_terminal(settings, pool):
    goal = repo.create_goal(pool, "Cancellable goal")
    agent = repo.register_agent(pool, "backend", "dev")
    issue = repo.create_issue(pool, goal.id, "Implement: x", team="backend")
    repo.claim_issue(pool, issue.id, agent.id)
    cancelled = repo.cancel_issue(pool, issue.id, reason="superseded", actor="operator")
    assert cancelled.state == IssueState.CANCELLED.value
    assert repo.get_agent(pool, agent.id).status == "idle"
    events = [e for e in repo.recent_events(pool, issue.id, limit=50)
              if e.event_type == "cancelled"]
    assert events and events[0].payload.get("reason") == "superseded"
    # terminal — a second cancel raises
    import pytest
    with pytest.raises(ValueError):
        repo.cancel_issue(pool, issue.id)


def test_cancelled_issue_lets_goal_reconcile(settings, pool):
    goal = repo.create_goal(pool, "Goal with one cancelled issue")
    issue = repo.create_issue(pool, goal.id, "Implement: x", team="backend")
    repo.set_goal_state(pool, goal.id, "active")
    repo.cancel_issue(pool, issue.id, reason="garbage")
    Engine(copy.deepcopy(settings), pool, reasoner=StubReasoner()).tick()
    # all issues terminal (cancelled) and none failed/off_rails → goal closes (done)
    assert repo.get_goal(pool, goal.id).state == "done"
