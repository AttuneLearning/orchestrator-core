"""Intake is lightweight admission, not an ADR-compliance verdict: even a reasoner
that declines everything cannot reject an issue at the intake gate."""

from __future__ import annotations

from orchestrator import repository as repo
from orchestrator.engine.loop import TickSummary
from orchestrator.models import IssueState

from test_engine import _DecliningReasoner, _make_engine


def test_intake_auto_admits_even_when_reasoner_declines(settings, pool):
    repo.register_agent(pool, "backend", "dev")
    goal = repo.create_goal(pool, "Backend thing", pipeline="pipeline-1")
    issue = repo.create_issue(pool, goal.id, "do thing", team="backend",
                              pipeline="pipeline-1")
    # Drive the issue to IN_REVIEW at the intake gate (skip planning for determinism).
    repo.update_state(pool, issue.id, IssueState.READY.value)
    repo.update_state(pool, issue.id, IssueState.IN_PROGRESS.value, gate_type="intake")
    repo.update_state(pool, issue.id, IssueState.IN_REVIEW.value, gate_type="intake")

    # A reasoner that declines every gate_review it is asked to render.
    engine = _make_engine(settings, pool, reasoner=_DecliningReasoner())
    engine._tick_rules = []  # tick() normally sets this; we call _advance directly
    engine._advance(TickSummary())

    got = repo.get_issue(pool, issue.id)
    # If intake consulted the reasoner it would have declined and stayed at intake.
    # Auto-admission advances it to the next gate instead.
    assert got.gate_type == "implementation", got.gate_type
    assert got.state != IssueState.FAILED.value
    # And no decline was recorded at intake.
    declines = [e for e in repo.recent_events(pool, issue.id, limit=20)
                if e.event_type == "gate_decline"]
    assert declines == []
