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


def select_assignable(
    ready: Iterable[Issue],
    in_progress: Iterable[Issue],
    maintenance_goal_ids: Iterable[int],
) -> list[Issue]:
    """Order this tick's READY issues for assignment, applying the maintenance
    backfill rule: a maintenance issue (one whose goal is a maintenance goal) is
    only assignable to a team that has NO standard work pending — i.e. no standard
    issue that is READY or IN_PROGRESS for that team. Standard work is returned
    first (oldest id first), then eligible maintenance work, so real work always
    claims idle workers before maintenance backfills the remainder. Pure."""
    maint = set(maintenance_goal_ids)
    ready = list(ready)

    def is_maint(i: Issue) -> bool:
        return i.goal_id in maint

    # Teams that still have standard (non-maintenance) work queued or in flight.
    busy_teams = {i.team for i in ready if not is_maint(i)}
    busy_teams |= {i.team for i in in_progress if not is_maint(i)}

    standard = sorted((i for i in ready if not is_maint(i)), key=lambda i: i.id)
    backfill = sorted((i for i in ready if is_maint(i) and i.team not in busy_teams),
                      key=lambda i: i.id)
    return standard + backfill


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
