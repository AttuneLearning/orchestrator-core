"""Pure tests for mechanical focus signals and the off-rails latch rule."""

from orchestrator.config import Thresholds
from orchestrator.engine import focus, offrails
from orchestrator.models import Issue, IssueEvent


def _issue(**kw) -> Issue:
    base = dict(id=1, goal_id=1, title="I")
    base.update(kw)
    return Issue(**base)


def _events(specs) -> list[IssueEvent]:
    return [
        IssueEvent(id=i, issue_id=1, seq=i, event_type=et, to_state=to)
        for i, (et, to) in enumerate(specs, start=1)
    ]


T = Thresholds(drift_threshold=0.5, retry_cap=3, step_budget=25)


def test_no_signals_when_healthy():
    assert focus.mechanical_signals(_issue(retry_count=0, step_count=1), [], T) == []


def test_retry_cap_signal():
    sig = focus.mechanical_signals(_issue(retry_count=3), [], T)
    assert "retry_cap" in sig


def test_step_budget_signal():
    sig = focus.mechanical_signals(_issue(step_count=25), [], T)
    assert "step_budget" in sig


def test_repeated_errors_signal():
    evs = _events([("error", None)] * 3)
    assert "repeated_errors" in focus.mechanical_signals(_issue(), evs, T)


def test_oscillation_signal():
    evs = _events([("gate_decline", "implementation")] * 3)
    assert "oscillation" in focus.mechanical_signals(_issue(), evs, T)


def test_quarantine_requires_signal_and_low_drift():
    # signal present but on-track → no quarantine
    assert offrails.should_quarantine(["retry_cap"], drift_score=0.9, drift_threshold=0.5) is False
    # signal present and adrift → quarantine
    assert offrails.should_quarantine(["retry_cap"], drift_score=0.2, drift_threshold=0.5) is True
    # adrift but no signal → no quarantine
    assert offrails.should_quarantine([], drift_score=0.1, drift_threshold=0.5) is False


def test_fleet_focus():
    assert focus.fleet_focus(active=0, flagged=0) == 1.0
    assert focus.fleet_focus(active=4, flagged=1) == 0.75
    assert focus.fleet_focus(active=2, flagged=2) == 0.0
