"""Re-engagement: when an agent exhausts its working window (step budget), persist
a context snapshot to the event log and re-seed a fresh window so work resumes
without losing state.

For this phase an issue is re-engaged at most once; if it exhausts again the
step-budget focus signal stands and the off-rails / pause path takes over.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..models import Issue


def is_exhausted(issue: Issue, step_budget: int) -> bool:
    return issue.step_count >= step_budget


def already_reengaged(pool: ConnectionPool, issue_id: int) -> bool:
    return any(e.event_type == "reengaged" for e in repo.recent_events(pool, issue_id, limit=200))


def reengage(pool: ConnectionPool, issue: Issue) -> Issue:
    """Snapshot context, re-seed the window (reset step_count), and resume.

    The snapshot pulls the issue's scoped memory and recent events so a fresh
    window can be reconstructed from durable state in Postgres.
    """
    memory = [n.body for n in repo.memory_recall(pool, scope=f"agent:{issue.assigned_agent}", limit=10)]
    recent = [
        {"seq": e.seq, "event_type": e.event_type, "to_state": e.to_state}
        for e in repo.recent_events(pool, issue.id, limit=20)
    ]
    repo.append_log(
        pool,
        issue.id,
        "context_snapshot",
        {"issue_title": issue.title, "memory": memory, "recent": recent},
    )
    # Re-seed: reset the step window, keep gate/state, log the re-engagement.
    updated = repo.update_state(
        pool,
        issue.id,
        to_state=issue.state,
        gate_type=issue.gate_type,
        event_type="reengaged",
        payload={"reset_step_count_from": issue.step_count},
        step_count=0,
    )
    return updated
