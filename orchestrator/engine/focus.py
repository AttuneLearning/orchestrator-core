"""Focus: mechanical signals derived from an issue and its event log.

These signals are *mechanical* — cheap, deterministic checks over the
append-only issue_events log. They gate the (expensive) Code Drift Reviewer:
off-rails only latches when a mechanical signal fires AND the drift score is
low (see offrails.py). Pure functions; no DB access.
"""

from __future__ import annotations

from typing import Iterable

from ..config import Thresholds
from ..models import Issue, IssueEvent

REPEATED_ERROR_LIMIT = 3
OSCILLATION_LIMIT = 3


def mechanical_signals(
    issue: Issue, events: Iterable[IssueEvent], thresholds: Thresholds
) -> list[str]:
    """Return the names of any mechanical concern signals firing for this issue."""
    events = list(events)
    signals: list[str] = []

    if issue.retry_count >= thresholds.retry_cap:
        signals.append("retry_cap")
    if issue.step_count >= thresholds.step_budget:
        signals.append("step_budget")

    error_count = sum(1 for e in events if e.event_type == "error")
    if error_count >= REPEATED_ERROR_LIMIT:
        signals.append("repeated_errors")

    # State oscillation: the issue keeps bouncing back through gate declines.
    # A healthy issue accrues zero declines, so this never fires on the happy path.
    decline_count = sum(1 for e in events if e.event_type == "gate_decline")
    if decline_count >= OSCILLATION_LIMIT:
        signals.append("oscillation")

    return signals


def signals_after_directive(
    issue: Issue, events: Iterable[IssueEvent], thresholds: Thresholds
) -> list[str]:
    """mechanical_signals over only the events *after* the latest human directive.

    A directive is a fresh start, so prior declines/errors must not re-trip the
    quarantine. `events` is newest-first (repository.recent_events order); the
    same view feeds the engine sweep and the dashboard so they never disagree on
    what is flagged.
    """
    events = list(events)
    cut = next((i for i, e in enumerate(events) if e.event_type == "directive"), None)
    if cut is not None:
        events = events[:cut]
    return mechanical_signals(issue, events, thresholds)


def fleet_focus(active: int, flagged: int) -> float:
    """Fraction of active issues with no mechanical signal. 1.0 when all clear."""
    if active <= 0:
        return 1.0
    return max(0.0, (active - flagged) / active)
