"""Integration tests for sub-issue decomposition (slice E).

Tests 1-5: _maybe_decompose, _unblock, blocked→failed transitions,
TickSummary.subissues/unblocked fields.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from orchestrator import repository as repo
from orchestrator.agents.base import ComplexityAssessment, GateReview, IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.models import Goal, Issue


# --------------------------------------------------------------------------- #
# Custom reasoners
# --------------------------------------------------------------------------- #

class _DecomposingReasoner(StubReasoner):
    """Decomposes depth-0 issues whose title starts with 'Implement'; leaves others alone."""

    def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
        if issue.depth == 0 and issue.title.startswith("Implement"):
            return ComplexityAssessment(
                decompose=True,
                subissues=[IssueSpec("Part A"), IssueSpec("Part B")],
            )
        return ComplexityAssessment(decompose=False, subissues=[])


class _AlwaysDecomposingReasoner(StubReasoner):
    """Unconditionally wants to decompose every issue into 2 sub-issues."""

    def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
        return ComplexityAssessment(
            decompose=True,
            subissues=[IssueSpec("Sub-alpha"), IssueSpec("Sub-beta")],
        )


class _PartADecliningReasoner(_DecomposingReasoner):
    """Decomposes like _DecomposingReasoner; declines gate_review for issues titled 'Part A'."""

    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list[dict[str, Any]]] = None) -> GateReview:
        if issue.title == "Part A":
            return GateReview(passed=False, reasons=["Part A rejected by policy"])
        return GateReview(passed=True, reasons=[f"{gate_type} exit criteria met (stub)"])


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
# 1. test_decompose_blocks_parent_until_children_done
# --------------------------------------------------------------------------- #

def test_decompose_blocks_parent_until_children_done(settings, pool):
    """A decomposed parent becomes blocked, unblocks when children done, then completes."""
    goal = repo.create_goal(pool, "Big feature", "needs sub-issues")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=_DecomposingReasoner())
    engine.run()

    all_issues = repo.list_issues(pool, goal_id=goal.id)
    # StubReasoner decomposes the goal into 1 top-level "Implement:" issue;
    # _DecomposingReasoner further decomposes it into 2 children.
    assert len(all_issues) == 3, (
        f"expected 3 issues (1 top-level + 2 sub); got {len(all_issues)}: "
        f"{[i.title for i in all_issues]}"
    )

    parent = next(i for i in all_issues if i.title.startswith("Implement"))
    children = repo.list_issues(pool, parent_id=parent.id)

    # Parent has a "decomposed" event
    parent_event_types = _event_types(pool, parent.id)
    assert "decomposed" in parent_event_types, (
        f"parent missing 'decomposed' event; events: {parent_event_types}"
    )

    # Children have correct parent_id and depth
    for child in children:
        assert child.parent_id == parent.id, f"child {child.id} parent_id mismatch"
        assert child.depth == 1, f"child {child.id} depth={child.depth}, expected 1"
        assert child.team == parent.team, f"child {child.id} team mismatch"
        assert child.pipeline == parent.pipeline, f"child {child.id} pipeline mismatch"

    # All issues are done
    for issue in all_issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} ({issue.title!r}) state={refreshed.state!r}, expected 'done'"
        )

    # Goal is done
    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "done", f"goal state={row[0]!r}, expected 'done'"

    # Parent's timeline includes blocked→ready transition
    timeline = repo.issue_timeline(pool, parent.id)
    to_states = [e.to_state for e in timeline]
    assert "blocked" in to_states, f"parent never reached 'blocked'; timeline to_states: {to_states}"
    assert "ready" in to_states, f"parent never reached 'ready'; timeline to_states: {to_states}"

    # The blocked event comes before the ready event
    blocked_idx = next(i for i, e in enumerate(timeline) if e.to_state == "blocked")
    ready_idx = next(i for i, e in enumerate(timeline) if e.to_state == "ready")
    assert blocked_idx < ready_idx, "blocked should precede ready in timeline"


# --------------------------------------------------------------------------- #
# 2. test_depth_cap_prevents_grandchildren
# --------------------------------------------------------------------------- #

def test_depth_cap_prevents_grandchildren(settings, pool):
    """max_depth=1 prevents children from being decomposed further."""
    goal = repo.create_goal(pool, "Nested goal", "")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool,
                          reasoner=_AlwaysDecomposingReasoner(),
                          max_depth=1)
    engine.run()

    all_issues = repo.list_issues(pool, goal_id=goal.id)
    for issue in all_issues:
        assert issue.depth <= 1, (
            f"issue {issue.id} depth={issue.depth} exceeds max_depth=1"
        )

    # Everything still completes
    for issue in all_issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} ({issue.title!r}) state={refreshed.state!r}, expected 'done'"
        )


# --------------------------------------------------------------------------- #
# 3. test_goal_cap_stops_decomposition
# --------------------------------------------------------------------------- #

def test_goal_cap_stops_decomposition(settings, pool):
    """max_issues_per_goal=1 prevents any sub-issues being created.

    StubReasoner.decompose_goal creates one "Implement:" issue, which already
    reaches the cap, so _maybe_decompose must raise an alert event (no silent
    truncation) and let the parent proceed undecomposed.
    """
    goal = repo.create_goal(pool, "Big feature", "hit the cap")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool,
                          reasoner=_DecomposingReasoner(),
                          max_issues_per_goal=1)
    engine.run()

    all_issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(all_issues) == 1, (
        f"expected exactly 1 issue (no sub-issues created); got {len(all_issues)}: "
        f"{[i.title for i in all_issues]}"
    )

    # The would-be parent has an "alert" event with cap == "max_issues_per_goal"
    parent = next((i for i in all_issues if i.title.startswith("Implement")), None)
    assert parent is not None, "Implement issue not found"
    alert_events = [
        e for e in _events(pool, parent.id)
        if e.event_type == "alert" and e.payload.get("cap") == "max_issues_per_goal"
    ]
    assert len(alert_events) >= 1, (
        f"expected alert event with cap=='max_issues_per_goal'; "
        f"events: {[(e.event_type, e.payload) for e in _events(pool, parent.id)]}"
    )

    # Issues still complete (parent proceeds as a normal issue after the cap check)
    for issue in all_issues:
        refreshed = repo.get_issue(pool, issue.id)
        assert refreshed.state == "done", (
            f"issue {issue.id} ({issue.title!r}) state={refreshed.state!r}, expected 'done'"
        )


# --------------------------------------------------------------------------- #
# 4. test_failed_child_fails_parent_and_pauses_goal
# --------------------------------------------------------------------------- #

def test_failed_child_fails_parent_and_pauses_goal(settings, pool):
    """A child that hits retry_cap → failed causes the parent to fail and goal to pause."""
    goal = repo.create_goal(pool, "Big feature", "child will fail")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool,
                          reasoner=_PartADecliningReasoner(),
                          drift_threshold=1.0,  # never quarantine
                          retry_cap=2)
    engine.run()

    all_issues = repo.list_issues(pool, goal_id=goal.id)

    # "Part A" child is failed
    part_a = next((i for i in all_issues if i.title == "Part A"), None)
    assert part_a is not None, "Part A child not found"
    refreshed_a = repo.get_issue(pool, part_a.id)
    assert refreshed_a.state == "failed", (
        f"Part A state={refreshed_a.state!r}, expected 'failed'"
    )

    # Parent ("Implement: Big feature") is failed with a "failed_children" payload event
    parent = next((i for i in all_issues if i.title.startswith("Implement")), None)
    assert parent is not None, "Implement (parent) issue not found"
    refreshed_parent = repo.get_issue(pool, parent.id)
    assert refreshed_parent.state == "failed", (
        f"parent state={refreshed_parent.state!r}, expected 'failed'"
    )
    failed_child_events = [
        e for e in _events(pool, parent.id)
        if e.event_type == "state_change" and "failed_children" in e.payload
    ]
    assert len(failed_child_events) >= 1, (
        f"parent missing 'failed_children' payload event; "
        f"events: {[(e.event_type, e.payload) for e in _events(pool, parent.id)]}"
    )

    # Goal is paused
    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM goals WHERE id = %s", (goal.id,)).fetchone()
    assert row[0] == "paused", f"goal state={row[0]!r}, expected 'paused'"


# --------------------------------------------------------------------------- #
# 5. test_subissue_counters
# --------------------------------------------------------------------------- #

def test_subissue_counters(settings, pool):
    """TickSummary.subissues==2 appears in some tick; TickSummary.unblocked==1 in a later one."""
    goal = repo.create_goal(pool, "Big feature", "check counters")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=_DecomposingReasoner())
    history = engine.run()

    subissue_counts = [s.subissues for s in history]
    unblocked_counts = [s.unblocked for s in history]

    assert any(n == 2 for n in subissue_counts), (
        f"expected a tick with subissues==2; got {subissue_counts}"
    )
    assert any(n >= 1 for n in unblocked_counts), (
        f"expected a tick with unblocked>=1; got {unblocked_counts}"
    )

    # The unblocked tick comes after the subissues tick
    first_subissue_tick = next(i for i, s in enumerate(history) if s.subissues == 2)
    first_unblocked_tick = next(i for i, s in enumerate(history) if s.unblocked >= 1)
    assert first_subissue_tick < first_unblocked_tick, (
        f"subissues at tick {first_subissue_tick}, unblocked at tick {first_unblocked_tick}"
    )
