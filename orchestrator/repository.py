"""Repository: the single place where SQL lives.

Both the engine and the MCP tool layer mutate state exclusively through these
functions, so every state change is recorded in the append-only issue_events
log that off-rails / oscillation detection depends on.

State transitions are written via update_state(), which inserts a matching
issue_events row in the same transaction as the issues update.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .models import Agent, Goal, GoalState, Issue, IssueEvent, IssueState, MemoryNote
from .state_machine import validate_transition

# Module-level cache for pgvector availability.  None = not yet checked.
_pgvector_ok: Optional[bool] = None


def reset_pgvector_cache() -> None:
    """Reset the cached pgvector-availability flag.  Intended for tests only."""
    global _pgvector_ok
    _pgvector_ok = None


def _pgvector_available(pool: ConnectionPool) -> bool:
    """Return True iff pgvector is installed AND the embedding_v column exists.

    The result is cached for the lifetime of the process (module-level variable).
    Call reset_pgvector_cache() in tests that need a fresh check.
    """
    global _pgvector_ok
    if _pgvector_ok is not None:
        return _pgvector_ok
    try:
        with pool.connection() as conn:
            ext_row = conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            if ext_row is None:
                _pgvector_ok = False
                return False
            col_row = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'memory_notes' AND column_name = 'embedding_v'"
            ).fetchone()
            _pgvector_ok = col_row is not None
    except Exception:
        _pgvector_ok = False
    return _pgvector_ok


# --------------------------------------------------------------------------- #
# Goals
# --------------------------------------------------------------------------- #

_GOAL_COLS = (
    "id, title, description, state, pipeline, created_at, updated_at, "
    "suggested_by, source, decompose, kind"
)


def create_goal(pool: ConnectionPool, title: str, description: str = "",
                pipeline: str = "pipeline-1", *, state: str = "backlog",
                suggested_by: str = "", source: str = "",
                decompose: Optional[str] = None, kind: str = "standard") -> Goal:
    with pool.connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO goals (title, description, state, pipeline,
                               suggested_by, source, decompose, kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_GOAL_COLS}
            """,
            (title, description, state, pipeline, suggested_by, source, decompose, kind),
        ).fetchone()
    return Goal(*row)


def maintenance_goal_ids(pool: ConnectionPool) -> set[int]:
    """IDs of active maintenance goals — the standing backlogs whose issues only
    backfill idle team capacity (engine.focus.select_assignable)."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM goals WHERE kind = 'maintenance' "
            "AND state NOT IN ('done', 'cancelled', 'rejected')"
        ).fetchall()
    return {r[0] for r in rows}


def propose_goal(pool: ConnectionPool, title: str, description: str = "",
                 pipeline: str = "pipeline-1", suggested_by: str = "agent",
                 source: str = "", decompose: Optional[str] = None) -> Goal:
    """Create a goal in the gated 'suggested' state (the MCP propose_goal path).

    The engine's _decompose only acts on 'backlog' goals, so a suggested goal is
    inert until a human promotes it (promote_goal). This is the human-in-the-loop
    gate for work proposed by external looping agents.
    """
    return create_goal(pool, title, description, pipeline,
                        state="suggested", suggested_by=suggested_by, source=source,
                        decompose=decompose)


def promote_goal(pool: ConnectionPool, goal_id: int) -> None:
    """Human review: accept a suggested goal into the work queue (suggested → backlog)."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE goals SET state = 'backlog', updated_at = now() "
            "WHERE id = %s AND state = 'suggested' RETURNING id",
            (goal_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"goal {goal_id} not found or not in 'suggested' state")


def reject_goal(pool: ConnectionPool, goal_id: int) -> None:
    """Human review: decline a suggested goal (suggested → rejected)."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE goals SET state = 'rejected', updated_at = now() "
            "WHERE id = %s AND state = 'suggested' RETURNING id",
            (goal_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"goal {goal_id} not found or not in 'suggested' state")


def list_goals_by_state(pool: ConnectionPool, state: str) -> list[Goal]:
    """Goals in a single state, oldest first — used for the dashboard suggestions
    review section."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            f"SELECT {_GOAL_COLS} FROM goals WHERE state = %s ORDER BY id",
            (state,),
        ).fetchall()


def list_open_goals(pool: ConnectionPool) -> list[Goal]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            f"""
            SELECT {_GOAL_COLS}
            FROM goals
            WHERE state NOT IN ('done', 'paused', 'suggested', 'rejected')
            ORDER BY id
            """
        ).fetchall()


def list_all_goals(pool: ConnectionPool) -> list[Goal]:
    """Every goal regardless of state — for the dashboard, which must show paused
    and done goals that list_open_goals deliberately hides from the engine."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            f"SELECT {_GOAL_COLS} FROM goals ORDER BY id"
        ).fetchall()


def set_goal_state(pool: ConnectionPool, goal_id: int, state: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE goals SET state = %s, updated_at = now() WHERE id = %s",
            (state, goal_id),
        )


def get_goal(pool: ConnectionPool, goal_id: int) -> Optional[Goal]:
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_GOAL_COLS} FROM goals WHERE id = %s", (goal_id,)
        ).fetchone()
    return Goal(*row) if row else None


def complete_goal(pool: ConnectionPool, goal_id: int) -> None:
    """Human verdict: mark a goal done. For closing out work the orchestrator can't
    self-verify (the engine never reads repos) — e.g. a goal whose work is actually
    finished, or a stale goal being retired. Does not touch the goal's issues."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE goals SET state = 'done', updated_at = now() "
            "WHERE id = %s AND state <> 'done' RETURNING id",
            (goal_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"goal {goal_id} not found or already done")


def resume_goal(pool: ConnectionPool, goal_id: int) -> None:
    """Human directive: restart a paused goal (paused → active)."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE goals SET state = 'active', updated_at = now() "
            "WHERE id = %s AND state = 'paused' RETURNING id",
            (goal_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"goal {goal_id} not found or not paused")


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #

def create_issue(
    pool: ConnectionPool,
    goal_id: int,
    title: str,
    description: str = "",
    team: str = "backend",
    pipeline: str = "pipeline-1",
    parent_id: Optional[int] = None,
    depth: int = 0,
    triggered_by_message: bool = False,
    origin_message_id: Optional[int] = None,
) -> Issue:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            """
            INSERT INTO issues
                (goal_id, parent_id, depth, team, title, description, pipeline,
                 state, triggered_by_message, origin_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'backlog', %s, %s)
            RETURNING id, goal_id, title, description, parent_id, depth, team,
                      pipeline, state, gate_type, retry_count, step_count,
                      assigned_agent, triggered_by_message, origin_message_id,
                      work_type, created_at, updated_at
            """,
            (goal_id, parent_id, depth, team, title, description, pipeline,
             triggered_by_message, origin_message_id),
        ).fetchone()
        issue = _issue_from_row(row)
        _append_event(conn, issue.id, "created", None, issue.state, {"title": title})
    return issue


def create_subissue(
    pool: ConnectionPool,
    parent: Issue,
    title: str,
    description: str = "",
) -> Issue:
    """Create a child issue inheriting goal_id, with depth+1."""
    return create_issue(
        pool,
        goal_id=parent.goal_id,
        title=title,
        description=description,
        team=parent.team,
        pipeline=parent.pipeline,
        parent_id=parent.id,
        depth=parent.depth + 1,
    )


def get_issue(pool: ConnectionPool, issue_id: int) -> Optional[Issue]:
    with pool.connection() as conn:
        row = conn.execute(_ISSUE_SELECT + " WHERE id = %s", (issue_id,)).fetchone()
    return _issue_from_row(row) if row else None


def list_issues(
    pool: ConnectionPool,
    goal_id: Optional[int] = None,
    states: Optional[list[str]] = None,
    parent_id: Optional[int] = None,
    assigned_agent: Optional[int] = None,
) -> list[Issue]:
    clauses, params = [], []
    if goal_id is not None:
        clauses.append("goal_id = %s")
        params.append(goal_id)
    if states:
        clauses.append("state = ANY(%s)")
        params.append(states)
    if parent_id is not None:
        clauses.append("parent_id = %s")
        params.append(parent_id)
    if assigned_agent is not None:
        clauses.append("assigned_agent = %s")
        params.append(assigned_agent)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with pool.connection() as conn:
        rows = conn.execute(_ISSUE_SELECT + where + " ORDER BY id", params).fetchall()
    return [_issue_from_row(r) for r in rows]


def count_issues_for_goal(pool: ConnectionPool, goal_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM issues WHERE goal_id = %s", (goal_id,)
        ).fetchone()[0]


def set_work_type(pool: ConnectionPool, issue_id: int, work_type: str) -> None:
    """Tag an issue's detected work-type (drives ADR rule selection)."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE issues SET work_type = %s, updated_at = now() WHERE id = %s",
            (work_type, issue_id),
        )


def claim_issue(pool: ConnectionPool, issue_id: int, agent_id: int) -> None:
    with pool.connection() as conn, conn.transaction():
        # Team-affinity guard: a worker may only claim issues for its OWN team.
        # This is the hard invariant behind the soft "pick an issue whose team is
        # <team>" prompt rule — without it a model can grab another team's orphaned
        # issue (e.g. a frontend worker claiming a backend issue that was reclaimed
        # off a stale backend worker). 'senior' is a cross-team escalation role and
        # is exempt (it works pre-assigned issues across every lane).
        row = conn.execute(
            "SELECT i.team, a.team FROM issues i, agents a "
            "WHERE i.id = %s AND a.id = %s",
            (issue_id, agent_id),
        ).fetchone()
        if row is not None:
            issue_team, agent_team = row
            if agent_team != "senior" and issue_team != agent_team:
                raise ValueError(
                    f"agent {agent_id} (team '{agent_team}') may not claim issue "
                    f"{issue_id} (team '{issue_team}'): cross-team claim rejected"
                )
        conn.execute(
            "UPDATE issues SET assigned_agent = %s, updated_at = now() WHERE id = %s",
            (agent_id, issue_id),
        )
        # status=busy; last_seen=now() gives a freshly-claimed pull worker a full
        # stale-window grace period before liveness reclaim (see _reclaim).
        conn.execute(
            "UPDATE agents SET status = 'busy', last_seen = now() WHERE id = %s",
            (agent_id,),
        )
        _append_event(conn, issue_id, "state_change", None, None,
                      {"claimed_by": agent_id})


def agent_seconds_since_seen(pool: ConnectionPool, agent_id: int) -> Optional[float]:
    """Seconds since the agent's last_seen, measured on the DB clock. None if the
    agent is unknown or has never been seen (treated as stale by callers)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - last_seen)) FROM agents WHERE id = %s",
            (agent_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# Event types that mean a worker actually PRODUCED work on an issue (not mere
# state churn). The durable-worker side-car's orchestrator-authoritative work
# signal (plan §15) keys on these.
WORK_EVENT_TYPES = ("code_committed", "tests_run")


def agent_last_work_at(pool: ConnectionPool, agent_id: int):
    """Timestamp of the most recent WORK event (WORK_EVENT_TYPES) on any issue
    currently assigned to this agent, or None. Monotonic in practice (events only
    append). Plan §15: the side-car polls this as the authoritative "did agent N
    do work" signal — derived from the report_work/commit events the worker
    already records via MCP — instead of parsing a TICK RESULT marker out of the
    worker's reply. No new worker behavior, no schema change.

    NB (§15.6): attribution is by the issue's CURRENT assigned_agent; a work event
    on an issue reassigned mid-tick could mis-credit. Acceptable for the cadence
    decision this feeds; tighten to event-time actor if it ever matters."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT MAX(e.created_at) FROM issue_events e "
            "JOIN issues i ON e.issue_id = i.id "
            "WHERE i.assigned_agent = %s AND e.event_type = ANY(%s)",
            (agent_id, list(WORK_EVENT_TYPES)),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def unassign_issue(pool: ConnectionPool, issue_id: int) -> None:
    """Clear an issue's assigned agent (does not touch agent status). Used when a
    pull gate has no eligible external worker, so the gate isn't falsely 'owned'
    by a carried-over agent from the previous gate."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE issues SET assigned_agent = NULL, updated_at = now() WHERE id = %s",
            (issue_id,),
        )


def escalate_to_senior(pool: ConnectionPool, issue_id: int) -> Optional[int]:
    """G8/G6 (ADR-PROC-002): hand an issue to the senior dev lane. Assigns it to a
    registered senior/dev agent (the issue KEEPS its own team, so the senior worker
    still derives the code lane: backend→apps/api, frontend→apps/web). Returns the
    senior agent id assigned, or None if none is registered (then just unassign so
    the failing worker doesn't immediately re-grab it)."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT id FROM agents WHERE team = 'senior' AND function = 'dev' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "UPDATE issues SET assigned_agent = NULL, updated_at = now() WHERE id = %s",
                (issue_id,))
            return None
        agent_id = row[0]
        conn.execute(
            "UPDATE issues SET assigned_agent = %s, updated_at = now() WHERE id = %s",
            (agent_id, issue_id))
        _append_event(conn, issue_id, "escalated_to_senior", None, None,
                      {"agent": agent_id})
        return agent_id


def update_state(
    pool: ConnectionPool,
    issue_id: int,
    to_state: str,
    gate_type: Optional[str] = None,
    event_type: str = "state_change",
    payload: Optional[dict[str, Any]] = None,
    retry_count: Optional[int] = None,
    step_count: Optional[int] = None,
) -> Issue:
    """Update an issue's state and append a matching issue_events row atomically."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.execute("SELECT state FROM issues WHERE id = %s", (issue_id,)).fetchone()
        from_state = cur[0] if cur else None

        sets = ["state = %s", "gate_type = %s", "updated_at = now()"]
        params: list[Any] = [to_state, gate_type]
        if retry_count is not None:
            sets.append("retry_count = %s")
            params.append(retry_count)
        if step_count is not None:
            sets.append("step_count = %s")
            params.append(step_count)
        params.append(issue_id)

        row = conn.execute(
            f"UPDATE issues SET {', '.join(sets)} WHERE id = %s RETURNING "
            "id, goal_id, title, description, parent_id, depth, team, pipeline, "
            "state, gate_type, retry_count, step_count, assigned_agent, "
            "triggered_by_message, origin_message_id, work_type, created_at, updated_at",
            params,
        ).fetchone()
        _append_event(conn, issue_id, event_type, from_state, to_state, payload or {})
    return _issue_from_row(row)


def hold_decomposed_parent(
    pool: ConnectionPool,
    issue_id: int,
    *,
    event_type: str = "coordination_hold",
    payload: Optional[dict[str, Any]] = None,
) -> Issue:
    """Keep a decomposed issue in the coordination-parent hold state.

    A parent with children is not a worker deliverable.  This is an explicit
    lifecycle repair path for stale/admin-recovered states (including
    ``in_review``), so it intentionally does not use the ordinary transition
    validator: ``in_review -> blocked`` is not an autonomous gate transition.
    The child check is performed inside the same transaction as the update so
    callers cannot accidentally use this for a normal issue.
    """
    with pool.connection() as conn, conn.transaction():
        child_rows = conn.execute(
            "SELECT id FROM issues WHERE parent_id = %s ORDER BY id", (issue_id,)
        ).fetchall()
        if not child_rows:
            raise ValueError(f"issue {issue_id} is not a decomposed parent")
        child_ids = [row[0] for row in child_rows]
        cur = conn.execute("SELECT state FROM issues WHERE id = %s", (issue_id,)).fetchone()
        if cur is None:
            raise ValueError(f"no issue {issue_id}")
        detail = dict(payload or {})
        detail.update({"reason": "decomposed parent is coordination-only",
                       "children": child_ids})
        row = conn.execute(
            "UPDATE issues SET state = %s, gate_type = NULL, assigned_agent = NULL, "
            "updated_at = now() WHERE id = %s RETURNING "
            "id, goal_id, title, description, parent_id, depth, team, pipeline, "
            "state, gate_type, retry_count, step_count, assigned_agent, "
            "triggered_by_message, origin_message_id, work_type, created_at, updated_at",
            (IssueState.BLOCKED.value, issue_id),
        ).fetchone()
        _append_event(conn, issue_id, event_type, cur[0], IssueState.BLOCKED.value, detail)
    return _issue_from_row(row)


def complete_decomposed_parent(
    pool: ConnectionPool,
    issue_id: int,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> Issue:
    """Close a coordination parent after every child is complete."""
    with pool.connection() as conn, conn.transaction():
        child_rows = conn.execute(
            "SELECT id, state FROM issues WHERE parent_id = %s ORDER BY id",
            (issue_id,),
        ).fetchall()
        if not child_rows:
            raise ValueError(f"issue {issue_id} is not a decomposed parent")
        if any(state != IssueState.DONE.value for _, state in child_rows):
            raise ValueError(f"decomposed parent {issue_id} has unfinished children")
        cur = conn.execute("SELECT state FROM issues WHERE id = %s", (issue_id,)).fetchone()
        if cur is None:
            raise ValueError(f"no issue {issue_id}")
        detail = dict(payload or {})
        detail.update({"reason": "all decomposed children complete",
                       "children": [row[0] for row in child_rows]})
        row = conn.execute(
            "UPDATE issues SET state = %s, gate_type = NULL, assigned_agent = NULL, "
            "updated_at = now() WHERE id = %s RETURNING "
            "id, goal_id, title, description, parent_id, depth, team, pipeline, "
            "state, gate_type, retry_count, step_count, assigned_agent, "
            "triggered_by_message, origin_message_id, work_type, created_at, updated_at",
            (IssueState.DONE.value, issue_id),
        ).fetchone()
        _append_event(conn, issue_id, "coordination_complete", cur[0],
                      IssueState.DONE.value, detail)
    return _issue_from_row(row)


def apply_directive(
    pool: ConnectionPool,
    issue_id: int,
    directive: str = "resume",
    note: str = "",
    actor: str = "human",
) -> Issue:
    """Human directive: recover a latched issue (off_rails OR failed → in_progress).

    The only path out of the off_rails quarantine and the failed terminal latch.
    Resets retry/step counters and keeps the gate, so work resumes where it
    stopped. Recorded as a 'directive' event — the focus sweep only considers
    events after the latest directive, so the issue gets a genuinely fresh start.
    If the issue's parent goal was closed/paused by this failure, it is re-set to
    'active' so the recovered issue actually gets picked back up.
    """
    issue = get_issue(pool, issue_id)
    if issue is None:
        raise ValueError(f"no issue {issue_id}")
    _recoverable = (IssueState.OFF_RAILS.value, IssueState.FAILED.value)
    if issue.state not in _recoverable or not validate_transition(
        issue.state, IssueState.IN_PROGRESS.value, directive=True
    ):
        raise ValueError(
            f"directive '{directive}' not applicable: issue {issue_id} is "
            f"'{issue.state}', expected 'off_rails' or 'failed'"
        )
    children = list_issues(pool, parent_id=issue.id)
    # Directives recover worker deliverables, not coordination parents.  Keep
    # decomposed parents held while their children run; otherwise the engine
    # will re-enter them into a worker gate and recreate the review crash-loop.
    if children:
        recovered = hold_decomposed_parent(
            pool, issue_id, event_type="directive",
            payload={"directive": directive, "note": note, "actor": actor,
                     "held_state": IssueState.BLOCKED.value},
        )
    else:
        recovered = update_state(
            pool, issue_id, IssueState.IN_PROGRESS.value, gate_type=issue.gate_type,
            event_type="directive",
            payload={"directive": directive, "note": note, "actor": actor},
            retry_count=0, step_count=0,
        )
    # Re-activate a parent goal that reconcile closed/paused on this failure,
    # otherwise the recovered issue would sit in a non-open goal and never run.
    if issue.goal_id is not None:
        goal = get_goal(pool, issue.goal_id)
        if goal is not None and goal.state in (
            GoalState.DONE.value, GoalState.PAUSED.value,
        ):
            set_goal_state(pool, issue.goal_id, GoalState.ACTIVE.value)
    return recovered


def cancel_issue(
    pool: ConnectionPool,
    issue_id: int,
    reason: str = "",
    actor: str = "human",
) -> Issue:
    """Terminate an issue without completing it (operator/auto triage of garbage,
    misrouted, or superseded work). Cancellable from any non-done state; releases
    the assigned agent. No-op-raises if the issue is already done/cancelled."""
    issue = get_issue(pool, issue_id)
    if issue is None:
        raise ValueError(f"no issue {issue_id}")
    to_state = IssueState.CANCELLED.value
    if not validate_transition(issue.state, to_state):
        raise ValueError(
            f"cannot cancel issue {issue_id}: state '{issue.state}' is terminal")
    if issue.assigned_agent is not None:
        set_agent_status(pool, issue.assigned_agent, "idle")
    return update_state(
        pool, issue_id, to_state, gate_type=issue.gate_type,
        event_type="cancelled", payload={"reason": reason, "actor": actor},
    )


def append_log(
    pool: ConnectionPool,
    issue_id: int,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Append a non-transition event (e.g. code_generated, error, drift_score)."""
    with pool.connection() as conn, conn.transaction():
        _append_event(conn, issue_id, event_type, None, None, payload or {})


def recent_events(pool: ConnectionPool, issue_id: int, limit: int = 50) -> list[IssueEvent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            """
            SELECT id, issue_id, seq, event_type, from_state, to_state, payload, created_at
            FROM issue_events
            WHERE issue_id = %s
            ORDER BY seq DESC
            LIMIT %s
            """,
            (issue_id, limit),
        ).fetchall()
    return [IssueEvent(**r) for r in rows]


def issue_timeline(pool: ConnectionPool, issue_id: int) -> list[IssueEvent]:
    """All events for an issue in chronological (seq ascending) order — for the
    dashboard timeline. Distinct from recent_events, which is newest-first."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT id, issue_id, seq, event_type, from_state, to_state, payload, created_at "
            "FROM issue_events WHERE issue_id = %s ORDER BY seq ASC",
            (issue_id,),
        ).fetchall()
    return [IssueEvent(**r) for r in rows]


def events_since(pool: ConnectionPool, after_id: int = 0,
                 limit: int = 200) -> list[IssueEvent]:
    """Cross-issue event feed for polling clients, oldest-first by global id.

    issue_events.id is GENERATED ALWAYS AS IDENTITY, so it is a globally
    monotonic cursor: pass the highest id you have seen as after_id to get only
    newer events. Distinct from recent_events (single issue, newest-first)."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            """
            SELECT id, issue_id, seq, event_type, from_state, to_state, payload, created_at
            FROM issue_events
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
            """,
            (after_id, limit),
        ).fetchall()
    return [IssueEvent(**r) for r in rows]


# Event types that represent a "major action" assigned to / taken by an agent.
_AGENT_ACTIVITY_TYPES = (
    "state_change", "gate_enter", "gate_pass", "gate_decline", "code_committed",
    "tests_run", "reclaimed", "directive", "code_generated", "comms_response",
    "verify_flaky",
)


def _activity_label(row: dict[str, Any]) -> str:
    et = row["event_type"]
    p = row.get("payload") or {}
    if et == "state_change":
        return "assigned (claimed)"  # only claim state_changes reach here
    if et == "gate_enter":
        return "reassigned" if "reassigned_to" in p else "entered gate"
    return {
        "gate_pass": "gate passed", "gate_decline": "gate declined",
        "code_committed": "committed code", "tests_run": "ran tests",
        "reclaimed": "reclaimed (went stale)", "directive": "resumed (directive)",
        "code_generated": "generated code", "comms_response": "sent response",
        "verify_flaky": "verify flaky (green tests, nonzero exit)",
    }.get(et, et)


def recent_agent_activity(pool: ConnectionPool, limit: int = 10) -> list[dict[str, Any]]:
    """The latest 'major actions' assigned to or taken by agents, newest-first.

    Agent attribution comes from the event payload (claimed_by / reassigned_to /
    agent) when present, else the issue's current assigned_agent. Read-only over
    issue_events — no schema change."""
    sql = """
        SELECT e.id, e.created_at, e.event_type, e.issue_id, e.payload,
               i.title AS issue_title,
               COALESCE((e.payload->>'claimed_by')::int,
                        (e.payload->>'reassigned_to')::int,
                        (e.payload->>'agent')::int,
                        i.assigned_agent) AS agent_id,
               a.team, a.function
        FROM issue_events e
        JOIN issues i ON i.id = e.issue_id
        LEFT JOIN agents a ON a.id = COALESCE((e.payload->>'claimed_by')::int,
                  (e.payload->>'reassigned_to')::int, (e.payload->>'agent')::int,
                  i.assigned_agent)
        WHERE e.event_type = ANY(%s)
          AND (e.event_type <> 'state_change' OR e.payload->>'claimed_by' IS NOT NULL)
        ORDER BY e.id DESC
        LIMIT %s
    """
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(sql, (list(_AGENT_ACTIVITY_TYPES), limit)).fetchall()
    for r in rows:
        r["action"] = _activity_label(r)
    return rows


def issue_tree(pool: ConnectionPool, goal_id: int) -> list[Issue]:
    """Issues for a goal, ordered so parents precede their children (depth, id)."""
    with pool.connection() as conn:
        rows = conn.execute(
            _ISSUE_SELECT + " WHERE goal_id = %s ORDER BY depth, id", (goal_id,)
        ).fetchall()
    return [_issue_from_row(r) for r in rows]


def count_by_state(pool: ConnectionPool, table: str) -> dict[str, int]:
    """{state: count} for goals or issues. `table` is a fixed literal, not user input."""
    if table not in ("goals", "issues"):
        raise ValueError(f"unsupported table {table!r}")
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT state, count(*) FROM {table} GROUP BY state"
        ).fetchall()
    return {state: n for state, n in rows}


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #

_AGENT_COLS = ("id, team, function, runtime, status, last_seen, created_at, "
               "loop_enabled, poll_interval_seconds, paused_until, "
               "active_window_seconds, dormant_interval_seconds")


def register_agent(
    pool: ConnectionPool, team: str, function: str = "dev", runtime: str = "api"
) -> Agent:
    with pool.connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO agents (team, function, runtime, status)
            VALUES (%s, %s, %s, 'idle')
            RETURNING {_AGENT_COLS}
            """,
            (team, function, runtime),
        ).fetchone()
    return Agent(*row)


def get_agent(pool: ConnectionPool, agent_id: int) -> Optional[Agent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        return cur.execute(
            f"SELECT {_AGENT_COLS} FROM agents WHERE id = %s", (agent_id,)
        ).fetchone()


def list_agents(pool: ConnectionPool, team: Optional[str] = None) -> list[Agent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        if team:
            return cur.execute(
                f"SELECT {_AGENT_COLS} FROM agents WHERE team = %s ORDER BY id",
                (team,),
            ).fetchall()
        return cur.execute(
            f"SELECT {_AGENT_COLS} FROM agents ORDER BY id"
        ).fetchall()


def set_agent_status(pool: ConnectionPool, agent_id: int, status: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE agents SET status = %s WHERE id = %s", (status, agent_id))


# Durable-worker side-car status self-report (migration 0024, plan §7) mapped
# to the DB's status vocabulary. 'dormant' needs no schema change: agents.status
# is plain unconstrained TEXT (0001_init.sql documents idle|busy|offline in a
# comment only, no CHECK constraint).
_HEARTBEAT_STATUS_MAP = {"working": "busy", "idle": "idle", "dormant": "dormant"}


def touch_agent(pool: ConnectionPool, agent_id: int, status: Optional[str] = None) -> None:
    """Heartbeat: record that the agent did work — or merely polled — just now.
    A worker that is talking to the coordinator is alive by definition, so this
    also REVIVES an agent the reclaim sweep latched to 'offline' (back to 'idle').
    Without the revive, a worker whose only activity is polling my_queue would be
    marked offline once and never recovered, and the _assign scan would refuse to
    route work to it — the pull-gate liveness deadlock.

    `status` (migration 0024): an optional durable-worker side-car self-report —
    one of 'working' | 'idle' | 'dormant' — mapped verbatim onto the DB status
    column (working->busy, idle->idle, dormant->dormant). This is the side-car's
    OWN authoritative claim about its worker, so when given it OVERRIDES the
    revive-only default below rather than merely reviving offline->idle: e.g. a
    'dormant' agent whose next heartbeat carries status='working' becomes
    'busy'. Old callers that never pass `status` (run-agent-loop.sh's curl, the
    MCP heartbeat/list_my_work/my_queue tools) get byte-identical behavior to
    before this migration. An unrecognized string is treated exactly like no
    status at all — this never raises, so a caller (e.g. the dashboard
    heartbeat endpoint) can pass untrusted input straight through and still
    always get a live heartbeat."""
    mapped = _HEARTBEAT_STATUS_MAP.get(status) if status else None
    with pool.connection() as conn:
        if mapped is not None:
            conn.execute(
                "UPDATE agents SET last_seen = now(), status = %s WHERE id = %s",
                (mapped, agent_id),
            )
        else:
            conn.execute(
                "UPDATE agents SET last_seen = now(), "
                "status = CASE WHEN status = 'offline' THEN 'idle' ELSE status END "
                "WHERE id = %s",
                (agent_id,),
            )


def agent_next_poll_seconds(agent: Agent) -> int:
    """Idle cadence the worker should obey: the poll interval when looping is
    enabled, else 0 — meaning 'stop after the queue drains' (disabled = stop)."""
    return agent.poll_interval_seconds if agent.loop_enabled else 0


def set_agent_loop(pool: ConnectionPool, agent_id: int, *,
                   loop_enabled: Optional[bool] = None,
                   poll_interval_seconds: Optional[int] = None,
                   active_window_seconds: Optional[int] = None,
                   dormant_interval_seconds: Optional[int] = None) -> Optional[Agent]:
    """Set a pull worker's loop policy. Only provided fields change.
    poll_interval_seconds is bounded to 60..7200s. active_window_seconds /
    dormant_interval_seconds (migration 0024, plan §7) are the durable-worker
    side-car cadence-window overrides, bounded to 300..14400s / 600..86400s
    respectively — the same ranges the side-car's own _coerce_policy applies
    when it reads these back, so a value that passes here can never be
    rejected/clamped on the side-car end."""
    sets: list[str] = []
    params: list[Any] = []
    if loop_enabled is not None:
        sets.append("loop_enabled = %s"); params.append(loop_enabled)
    if poll_interval_seconds is not None:
        if not (60 <= poll_interval_seconds <= 7200):
            raise ValueError("poll_interval_seconds must be between 60 and 7200")
        sets.append("poll_interval_seconds = %s"); params.append(poll_interval_seconds)
    if active_window_seconds is not None:
        if not (300 <= active_window_seconds <= 14400):
            raise ValueError("active_window_seconds must be between 300 and 14400")
        sets.append("active_window_seconds = %s"); params.append(active_window_seconds)
    if dormant_interval_seconds is not None:
        if not (600 <= dormant_interval_seconds <= 86400):
            raise ValueError("dormant_interval_seconds must be between 600 and 86400")
        sets.append("dormant_interval_seconds = %s"); params.append(dormant_interval_seconds)
    if sets:
        params.append(agent_id)
        with pool.connection() as conn:
            conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = %s", params)
    return get_agent(pool, agent_id)


def set_agent_pause(pool: ConnectionPool, agent_id: int,
                    paused_until: Optional[datetime]) -> Optional[Agent]:
    """Set (or clear, with None) an agent's cooldown window. While paused_until is
    in the future the engine won't assign it work and a pull worker sleeps until
    then. Used for token-limit backoff (now()+2h) and manual dashboard pauses."""
    with pool.connection() as conn:
        conn.execute("UPDATE agents SET paused_until = %s WHERE id = %s",
                     (paused_until, agent_id))
    return get_agent(pool, agent_id)


def get_wake_at(pool: ConnectionPool, project: str) -> Optional[datetime]:
    """The most recent wake signal for `project` (migration 0024, plan §7), or
    None if it has never been bumped. Every side-car polling
    GET /agents/{id}/pause?project=P compares this against the last value it
    observed and fires an immediate tick on increase — see
    Sidecar.check_wake."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT wake_at FROM wake_signal WHERE project = %s", (project,)
        ).fetchone()
    return row[0] if row else None


def bump_wake(pool: ConnectionPool, project: str) -> datetime:
    """Signal every side-car for `project` to wake immediately (dashboard "Wake
    all" button, or the orch-manager after promoting new work). UPSERT so the
    first bump for a never-before-seen project still works. Returns the new
    wake_at."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO wake_signal (project, wake_at) VALUES (%s, now()) "
            "ON CONFLICT (project) DO UPDATE SET wake_at = now() "
            "RETURNING wake_at",
            (project,),
        ).fetchone()
    return row[0]


def find_idle_agent(
    pool: ConnectionPool, team: str, function: Optional[str] = None,
    runtime: Optional[str] = None, include_offline: bool = False,
) -> Optional[Agent]:
    """Pick an available agent for a team. `function` (dev/qa/lead) and `runtime`
    (api/cli/external) narrow the search; idle agents rank ahead of busy ones.
    Pull gates pass runtime='external' to require a live worker. `include_offline`
    drops the online filter so a caller can still find the (currently down)
    owner-worker to PIN a queued issue to it rather than leave it unowned; most
    recently-seen ranks first among otherwise-equal candidates."""
    clauses = ["team = %s", "(paused_until IS NULL OR paused_until <= now())"]
    if not include_offline:
        clauses.append("status != 'offline'")
    params: list[Any] = [team]
    if function:
        clauses.append("function = %s")
        params.append(function)
    if runtime:
        clauses.append("runtime = %s")
        params.append(runtime)
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        return cur.execute(
            f"SELECT {_AGENT_COLS} FROM agents WHERE {' AND '.join(clauses)} "
            "ORDER BY (status = 'idle') DESC, last_seen DESC NULLS LAST, id LIMIT 1",
            params,
        ).fetchone()


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #

def memory_write(
    pool: ConnectionPool,
    body: str,
    scope: str = "global",
    embedding: Optional[list[float]] = None,
) -> MemoryNote:
    if embedding is not None and _pgvector_available(pool):
        vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO memory_notes (scope, body, embedding_v) "
                "VALUES (%s, %s, %s::vector) "
                "RETURNING id, scope, body, created_at",
                (scope, body, vec_literal),
            ).fetchone()
    else:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO memory_notes (scope, body) VALUES (%s, %s) "
                "RETURNING id, scope, body, created_at",
                (scope, body),
            ).fetchone()
    return MemoryNote(*row)


def memory_recall(pool: ConnectionPool, scope: str = "global", limit: int = 20) -> list[MemoryNote]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(MemoryNote))
        return cur.execute(
            "SELECT id, scope, body, created_at FROM memory_notes "
            "WHERE scope = %s ORDER BY id DESC LIMIT %s",
            (scope, limit),
        ).fetchall()


def _scope_filter(scope: Optional[str]) -> tuple[str, str]:
    """SQL condition + bind param for scoping a memory search.

    scope given  -> restrict to exactly that scope (e.g. an isolated KB).
    scope None   -> exclude the reserved private 'monitor:%' namespace, so a
                    general/agent search never surfaces the orch-monitor KB.
    """
    if scope is not None:
        return "scope = %s", scope
    return "scope NOT LIKE %s", "monitor:%"


def memory_search(
    pool: ConnectionPool,
    query: str,
    limit: int = 20,
    query_embedding: Optional[list[float]] = None,
    scope: Optional[str] = None,
) -> list[MemoryNote]:
    """Search memory notes.

    If query_embedding is provided AND pgvector is available, uses cosine
    distance (embedding_v <=> %s::vector) with a WHERE embedding_v IS NOT NULL
    filter.  Falls back to ILIKE on the query string for rows with no vector,
    appending any non-duplicate ILIKE hits up to the limit.

    When pgvector is unavailable or query_embedding is None, uses ILIKE only
    (original behaviour — all existing call sites continue to work).

    scope: restrict to one scope (isolated retrieval, e.g. 'monitor:kb'); when
    omitted, the reserved 'monitor:%' namespace is excluded so it stays private.
    """
    scope_sql, scope_param = _scope_filter(scope)
    if query_embedding is not None and _pgvector_available(pool):
        vec_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"
        with pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(MemoryNote))
            # Primary: vector-similarity results (rows that have an embedding).
            vec_rows = cur.execute(
                "SELECT id, scope, body, created_at FROM memory_notes "
                f"WHERE embedding_v IS NOT NULL AND {scope_sql} "
                "ORDER BY embedding_v <=> %s::vector "
                "LIMIT %s",
                (scope_param, vec_literal, limit),
            ).fetchall()
            seen_ids = {n.id for n in vec_rows}
            results = list(vec_rows)
            # Supplement with ILIKE hits if we still have room and the query
            # text is not empty (degenerate queries fall back entirely to ILIKE).
            if len(results) < limit and query:
                remaining = limit - len(results)
                ilike_rows = cur.execute(
                    "SELECT id, scope, body, created_at FROM memory_notes "
                    f"WHERE body ILIKE %s AND {scope_sql} ORDER BY id DESC LIMIT %s",
                    (f"%{query}%", scope_param, remaining + len(seen_ids)),
                ).fetchall()
                for n in ilike_rows:
                    if n.id not in seen_ids and len(results) < limit:
                        results.append(n)
        return results
    # Fallback: ILIKE behaviour (scope-aware).
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(MemoryNote))
        return cur.execute(
            "SELECT id, scope, body, created_at FROM memory_notes "
            f"WHERE body ILIKE %s AND {scope_sql} ORDER BY id DESC LIMIT %s",
            (f"%{query}%", scope_param, limit),
        ).fetchall()


def memory_clear_scope(pool: ConnectionPool, scope: str) -> int:
    """Delete all notes in a scope (for idempotent KB rebuilds). Returns count."""
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM memory_notes WHERE scope = %s", (scope,))
        return cur.rowcount


def get_system_state(pool: ConnectionPool, key: str) -> Optional[str]:
    with pool.connection() as conn:
        row = conn.execute("SELECT value FROM system_state WHERE key = %s",
                           (key,)).fetchone()
    return row[0] if row else None


def set_system_state(pool: ConnectionPool, key: str, value: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO system_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )


_DAEMON_HEARTBEAT_KEY = "daemon_heartbeat"


def record_daemon_heartbeat(pool: ConnectionPool) -> None:
    """Stamp the daemon's liveness (server clock). The multi-coordinator dashboard
    reads each instance's heartbeat age to show live / idle / unreachable."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO system_state (key, value) VALUES (%s, now()::text) "
            "ON CONFLICT (key) DO UPDATE SET value = now()::text, updated_at = now()",
            (_DAEMON_HEARTBEAT_KEY,),
        )


def daemon_heartbeat_age_seconds(pool: ConnectionPool) -> Optional[float]:
    """Seconds since the daemon last ticked, by the DB's own clock (None if never)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM now() - value::timestamptz) FROM system_state "
            "WHERE key = %s", (_DAEMON_HEARTBEAT_KEY,),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


# --------------------------------------------------------------------------- #
# ADRs & messages (skill-tool backing)
# --------------------------------------------------------------------------- #

_ADR_COLS = ["id", "adr_key", "domain", "title", "status", "decision", "context",
             "applies_to", "related", "supersedes", "patterns", "proposed_by",
             "created_at"]
_ADR_SELECT = ("SELECT id, adr_key, domain, title, status, decision, context, "
               "applies_to, related, supersedes, patterns, proposed_by, created_at "
               "FROM adrs")


def create_adr(
    pool: ConnectionPool,
    domain: str,
    title: str,
    decision: str = "",
    context: str = "",
    *,
    applies_to: Optional[dict[str, Any]] = None,
    related: Optional[list[str]] = None,
    supersedes: Optional[list[str]] = None,
    patterns: Optional[list[str]] = None,
    status: str = "accepted",
    proposed_by: str = "human",
) -> dict[str, Any]:
    """Create an ADR rule. decision = the compact directive agents receive;
    context = rationale (humans only). status='proposed' rules are inert until
    approve_adr promotes them."""
    # Normalize domain: strip and lowercase for case-insensitive matching and
    # consistent storage. The output key uses domain.upper() — unchanged.
    domain = domain.strip().lower()
    with pool.connection() as conn, conn.transaction():
        # Next number = max existing suffix in this domain + 1, NOT count(*).
        # Count-based numbering collides after a delete/supersede (the trailing
        # number can already be in use); adr_key is UNIQUE so that would error.
        # Use lower(domain) in the WHERE clause for case-insensitive matching
        # against pre-existing mixed-case rows.
        n = conn.execute(
            "SELECT COALESCE(MAX(substring(adr_key from '[0-9]+$')::int), 0) "
            "FROM adrs WHERE lower(domain) = %s", (domain,)
        ).fetchone()[0]
        adr_key = f"ADR-{domain.upper()}-{n + 1:03d}"
        row = conn.execute(
            "INSERT INTO adrs (adr_key, domain, title, decision, context, status, "
            "applies_to, related, supersedes, patterns, proposed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {', '.join(_ADR_COLS)}",
            (adr_key, domain, title, decision, context, status,
             Jsonb(applies_to or {}), related or [], supersedes or [],
             patterns or [], proposed_by),
        ).fetchone()
    return dict(zip(_ADR_COLS, row))


def list_adrs(pool: ConnectionPool, status: Optional[str] = None,
              domain: Optional[str] = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if domain:
        clauses.append("lower(domain) = %s")
        params.append(domain.strip().lower())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with pool.connection() as conn:
        rows = conn.execute(_ADR_SELECT + where + " ORDER BY adr_key", params).fetchall()
    return [dict(zip(_ADR_COLS, r)) for r in rows]


def get_adr(pool: ConnectionPool, adr_key: str) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(_ADR_SELECT + " WHERE adr_key = %s", (adr_key,)).fetchone()
    return dict(zip(_ADR_COLS, row)) if row else None


def worker_tier_stats(pool: ConnectionPool) -> list[dict[str, Any]]:
    """GAP-5: per (runtime, team) performance from the server-side stamps on work
    events — commits, machine verify runs (pass rate + avg duration), gate outcomes.
    Feeds the ADR-PROC-003 capability-ladder promotion/demotion decisions."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT COALESCE(payload->>'agent_runtime','?') AS runtime, "
            "       COALESCE(payload->>'agent_team','?') AS team, "
            "       COUNT(*) FILTER (WHERE event_type = 'code_committed') AS commits, "
            "       COUNT(*) FILTER (WHERE event_type = 'tests_run' "
            "                        AND (payload->>'machine')::bool) AS verifies, "
            "       COUNT(*) FILTER (WHERE event_type = 'tests_run' "
            "                        AND (payload->>'machine')::bool "
            "                        AND (CASE WHEN payload ? 'tests_failed_n' "
            "                                  THEN (payload->>'tests_failed_n')::int = 0 "
            "                                  ELSE (payload->>'returncode')::int = 0 END)) AS verify_green, "
            "       COUNT(*) FILTER (WHERE event_type = 'verify_flaky') AS verify_flaky, "
            "       ROUND(AVG((payload->>'duration_s')::float) FILTER "
            "             (WHERE payload ? 'duration_s')::numeric, 1) AS avg_verify_s, "
            "       COUNT(*) FILTER (WHERE event_type = 'gate_pass') AS gate_pass, "
            "       COUNT(*) FILTER (WHERE event_type = 'gate_decline') AS gate_decline "
            "FROM issue_events "
            "WHERE payload ? 'agent_runtime' "
            "  AND event_type IN ('code_committed','tests_run','gate_pass','gate_decline','verify_flaky') "
            "GROUP BY 1, 2 ORDER BY 2, 1"
        ).fetchall()
    cols = ["runtime", "team", "commits", "verifies", "verify_green", "verify_flaky",
            "avg_verify_s", "gate_pass", "gate_decline"]
    return [dict(zip(cols, r)) for r in rows]


def recent_adr_proposal_count(pool: ConnectionPool, proposed_by: str,
                              within_minutes: int = 60) -> int:
    """How many ADRs this proposer filed in the last `within_minutes`. Powers the
    G2 loop-breaker rate limit on adr_suggest — a wedged worker cannot spam the
    governance table (the ~1000 junk-ADR failure mode)."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM adrs WHERE proposed_by = %s "
            "AND created_at > now() - make_interval(mins => %s)",
            (proposed_by, within_minutes),
        ).fetchone()[0]


def approve_adr(pool: ConnectionPool, adr_key: str, actor: str = "human") -> dict[str, Any]:
    """Human gate: promote a proposed rule to accepted (it becomes live).

    Marks any ADRs this rule supersedes as 'superseded'."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE adrs SET status = 'accepted' "
            "WHERE adr_key = %s AND status = 'proposed' "
            f"RETURNING {', '.join(_ADR_COLS)}",
            (adr_key,),
        ).fetchone()
        if row is None:
            raise ValueError(f"ADR {adr_key} not found or not in 'proposed' status")
        adr = dict(zip(_ADR_COLS, row))
        for superseded_key in adr["supersedes"] or []:
            conn.execute(
                "UPDATE adrs SET status = 'superseded' "
                "WHERE adr_key = %s AND status = 'accepted'",
                (superseded_key,),
            )
    return adr


def deactivate_adr(pool: ConnectionPool, adr_key: str, actor: str = "human") -> dict[str, Any]:
    """Reverse of approve: send an accepted rule back to 'proposed' (it stops
    reaching agents immediately, but is kept for re-review)."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE adrs SET status = 'proposed' "
            "WHERE adr_key = %s AND status = 'accepted' "
            f"RETURNING {', '.join(_ADR_COLS)}",
            (adr_key,),
        ).fetchone()
        if row is None:
            raise ValueError(f"ADR {adr_key} not found or not in 'accepted' status")
        return dict(zip(_ADR_COLS, row))


def delete_adr(pool: ConnectionPool, adr_key: str) -> None:
    """Permanently delete a 'proposed' ADR. Accepted/superseded rules cannot be
    deleted (deactivate first) — keeps live governance from vanishing silently."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "DELETE FROM adrs WHERE adr_key = %s AND status = 'proposed' "
            "RETURNING adr_key",
            (adr_key,),
        ).fetchone()
        if row is None:
            raise ValueError(f"ADR {adr_key} not found or not in 'proposed' status")


def update_adr(pool: ConnectionPool, adr_key: str, *, title: Optional[str] = None,
               decision: Optional[str] = None, context: Optional[str] = None,
               applies_to: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Edit an ADR's content (the SoT edit path). Only the provided fields change;
    status is untouched, so an accepted rule stays live with corrected text. The
    single source of truth lives here — render-agent-docs regenerates CLAUDE.md/
    AGENTS.md from it."""
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = %s"); params.append(title)
    if decision is not None:
        sets.append("decision = %s"); params.append(decision)
    if context is not None:
        sets.append("context = %s"); params.append(context)
    if applies_to is not None:
        sets.append("applies_to = %s"); params.append(Jsonb(applies_to))
    if not sets:
        adr = get_adr(pool, adr_key)
        if adr is None:
            raise ValueError(f"ADR {adr_key} not found")
        return adr
    params.append(adr_key)
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            f"UPDATE adrs SET {', '.join(sets)} WHERE adr_key = %s "
            f"RETURNING {', '.join(_ADR_COLS)}",
            params,
        ).fetchone()
        if row is None:
            raise ValueError(f"ADR {adr_key} not found")
        return dict(zip(_ADR_COLS, row))


# -- pending actions: escalation persistence (migration 0022) --------------- #
_PENDING_ACTION_COLS = ["id", "issue_id", "worktree", "step", "action", "action_kind",
                        "requested_by", "status", "resolved_by", "created_at", "resolved_at",
                        "expires_at"]
_PENDING_ACTION_SELECT = ("SELECT id, issue_id, worktree, step, action, action_kind, "
                          "requested_by, status, resolved_by, created_at, resolved_at, expires_at "
                          "FROM pending_actions")


def create_pending_action(
    pool: ConnectionPool,
    *,
    issue_id: Optional[int],
    worktree: str,
    step: str,
    action: str,
    action_kind: str = "run",
    requested_by: str = "",
    ttl_hours: int = 24,
    phase: str = "",
) -> dict[str, Any]:
    """Create a pending action row awaiting approval. When issue_id is set,
    appends an 'action_escalated' event to that issue's timeline."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "INSERT INTO pending_actions (issue_id, worktree, step, action, action_kind, "
            "requested_by, expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {', '.join(_PENDING_ACTION_COLS)}",
            (issue_id, worktree, step, action, action_kind, requested_by, expires_at),
        ).fetchone()
        result = dict(zip(_PENDING_ACTION_COLS, row))
        if issue_id:
            payload = {
                "worktree": worktree,
                "step": step,
                "action": action,
                "action_kind": action_kind,
                "requested_by": requested_by,
                "expires_at": expires_at.isoformat(),
                "machine": True,
            }
            if phase:
                payload["phase"] = phase
            _append_event(conn, issue_id, "action_escalated", None, None, payload)
    return result


def list_pending_actions(pool: ConnectionPool, status: str = "pending") -> list[dict[str, Any]]:
    """List pending actions. First, lazily expire any overdue pending rows and
    emit 'action_expired' events for each (when issue_id is set). Create a
    re-escalation alert message for each expired action. Then return rows
    matching the requested status, ordered by created_at."""
    with pool.connection() as conn, conn.transaction():
        # Lazily expire overdue pending rows
        expired_rows = conn.execute(
            "UPDATE pending_actions SET status = 'expired', resolved_at = now() "
            "WHERE status = 'pending' AND expires_at < now() "
            f"RETURNING {', '.join(_PENDING_ACTION_COLS)}",
        ).fetchall()
        for row in expired_rows:
            expired_action = dict(zip(_PENDING_ACTION_COLS, row))
            if expired_action["issue_id"]:
                _append_event(conn, expired_action["issue_id"], "action_expired", None, None, {
                    "action_id": expired_action["id"],
                    "worktree": expired_action["worktree"],
                    "step": expired_action["step"],
                    "action": expired_action["action"],
                    "machine": True,
                })
                # Create re-escalation alert message
                action_str = expired_action["action"]
                subject = f"Action approval EXPIRED unanswered: {expired_action['step']}/{action_str[:60]}"
                body = (
                    f"Action approval expired unanswered after waiting period.\n"
                    f"Issue #{expired_action['issue_id']}, step '{expired_action['step']}'.\n"
                    f"Worktree: {expired_action['worktree']}\n"
                    f"Action: {action_str}\n"
                    f"Requested by: {expired_action['requested_by']}\n"
                    f"Created: {expired_action['created_at']}\n"
                    f"Expired: {expired_action['expires_at']}\n"
                    f"\n"
                    f"The step remains blocked. The approval will re-escalate when "
                    f"the worker attempts this step again."
                )
                conn.execute(
                    "INSERT INTO messages (from_team, to_team, subject, body, priority, "
                    "issue_id, kind, status, reply_to) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    ("engine", "orchestration", subject, body, "high", expired_action["issue_id"],
                     "request", "pending", None),
                )
        # Fetch all rows with the requested status
        rows = conn.execute(
            _PENDING_ACTION_SELECT + " WHERE status = %s ORDER BY created_at",
            (status,),
        ).fetchall()
    return [dict(zip(_PENDING_ACTION_COLS, r)) for r in rows]


def resolve_pending_action(
    pool: ConnectionPool,
    action_id: int,
    status: str,
    resolved_by: str,
) -> dict[str, Any]:
    """Resolve a pending action to 'approved' or 'denied', setting resolved_by
    and appending the corresponding event. Only transitions FROM 'pending' are
    allowed. Raises ValueError on invalid transition or unknown id."""
    if status not in ("approved", "denied"):
        raise ValueError(f"Invalid status {status!r}; must be 'approved' or 'denied'")
    event_type = "action_approved" if status == "approved" else "action_denied"
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE pending_actions SET status = %s, resolved_by = %s, resolved_at = now() "
            "WHERE id = %s AND status = 'pending' "
            f"RETURNING {', '.join(_PENDING_ACTION_COLS)}",
            (status, resolved_by, action_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Action {action_id} not found or not in 'pending' status")
        result = dict(zip(_PENDING_ACTION_COLS, row))
        if result["issue_id"]:
            _append_event(conn, result["issue_id"], event_type, None, None, {
                "action_id": result["id"],
                "worktree": result["worktree"],
                "step": result["step"],
                "action": result["action"],
                "resolved_by": resolved_by,
                "machine": True,
            })
    return result


def find_approved_action(
    pool: ConnectionPool,
    issue_id: int,
    step: str,
    action: str,
) -> Optional[dict[str, Any]]:
    """Find the newest 'approved' action matching the (issue_id, step, action)
    triple. Returns None if no match found."""
    with pool.connection() as conn:
        row = conn.execute(
            _PENDING_ACTION_SELECT + " "
            "WHERE issue_id = %s AND step = %s AND action = %s AND status = 'approved' "
            "ORDER BY created_at DESC LIMIT 1",
            (issue_id, step, action),
        ).fetchone()
    return dict(zip(_PENDING_ACTION_COLS, row)) if row else None


def consume_approved_action(pool: ConnectionPool, action_id: int) -> dict[str, Any]:
    """Consume an approved action by transitioning it to 'executed'. One-shot
    semantics: an approval unblocks exactly one run. Raises ValueError if the
    action is not in 'approved' status."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE pending_actions SET status = 'executed', resolved_at = now() "
            "WHERE id = %s AND status = 'approved' "
            f"RETURNING {', '.join(_PENDING_ACTION_COLS)}",
            (action_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Action {action_id} not found or not in 'approved' status")
        result = dict(zip(_PENDING_ACTION_COLS, row))
        if result["issue_id"]:
            _append_event(conn, result["issue_id"], "action_executed", None, None, {
                "action_id": result["id"],
                "worktree": result["worktree"],
                "step": result["step"],
                "action": result["action"],
                "machine": True,
            })
    return result


# -- issue <-> ADR relevance links (migration 0020) ------------------------- #

def set_issue_adrs(pool: ConnectionPool, issue_id: int, adr_keys: list[str],
                   source: str = "reasoner") -> None:
    """Record which accepted ADRs govern this issue (computed once at creation).
    Idempotent per (issue, adr_key); re-tagging updates the source."""
    with pool.connection() as conn, conn.transaction():
        for key in dict.fromkeys(adr_keys):  # dedupe, preserve order
            conn.execute(
                "INSERT INTO issue_adrs (issue_id, adr_key, source) VALUES (%s, %s, %s) "
                "ON CONFLICT (issue_id, adr_key) DO UPDATE SET source = EXCLUDED.source",
                (issue_id, key, source),
            )


def list_issue_adrs(pool: ConnectionPool, issue_id: int) -> list[str]:
    """The adr_keys previously related to this issue (reasoner/human tags)."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT adr_key FROM issue_adrs WHERE issue_id = %s ORDER BY adr_key",
            (issue_id,),
        ).fetchall()
    return [r[0] for r in rows]


def adrs_for_issue(pool: ConnectionPool, issue_id: int,
                   repos: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """The precise ADR surface for one issue: selector matches (team + auto/stored
    work_type + repos) UNION any reasoner-tagged keys, expanded via the full
    backlink closure. Uncapped; empty selectors/no tags degrade to project-wide
    rules only. This replaces `adr_list(status='accepted')` for pull workers."""
    from . import adr_rules
    issue = get_issue(pool, issue_id)
    if issue is None:
        return []
    accepted = list_adrs(pool, status="accepted")
    work_type = issue.work_type or adr_rules.detect_work_type(
        f"{issue.title}\n{issue.description or ''}")
    extra = list_issue_adrs(pool, issue_id)
    return adr_rules.relevant(accepted, work_type=work_type, team=issue.team or "",
                              repos=repos or [], extra_keys=extra)


# -- docs: shared cross-agent development docs (migration 0018) ------------- #
_DOC_COLS = ["id", "path", "title", "body", "format", "author",
             "created_at", "updated_at"]


def doc_upsert(pool: ConnectionPool, path: str, *, title: str = "", body: str = "",
               format: str = "markdown", author: str = "") -> dict[str, Any]:
    """Create or replace a doc at `path` (unique). On conflict, overwrites
    title/body/format/author and bumps updated_at — so an edit from the dashboard
    or an agent's doc_write is a single idempotent call."""
    with pool.connection() as conn:
        row = conn.execute(
            f"""INSERT INTO docs (path, title, body, format, author)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (path) DO UPDATE SET
                    title = EXCLUDED.title, body = EXCLUDED.body,
                    format = EXCLUDED.format, author = EXCLUDED.author,
                    updated_at = now()
                RETURNING {', '.join(_DOC_COLS)}""",
            (path, title, body, format, author),
        ).fetchone()
    return dict(zip(_DOC_COLS, row))


def doc_get(pool: ConnectionPool, path: str) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_DOC_COLS)} FROM docs WHERE path = %s", (path,),
        ).fetchone()
    return dict(zip(_DOC_COLS, row)) if row else None


def doc_list(pool: ConnectionPool) -> list[dict[str, Any]]:
    """All docs, newest-first. Body omitted from the list view for size."""
    cols = [c for c in _DOC_COLS if c != "body"]
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM docs ORDER BY updated_at DESC",
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def doc_search(pool: ConnectionPool, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Term search over title + body (ILIKE). Term overlap is the reliable signal
    at this corpus size — same rationale as monitor_kb retrieval."""
    cols = [c for c in _DOC_COLS if c != "body"]
    like = f"%{query}%"
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM docs "
            "WHERE title ILIKE %s OR body ILIKE %s ORDER BY updated_at DESC LIMIT %s",
            (like, like, limit),
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def doc_delete(pool: ConnectionPool, path: str) -> bool:
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM docs WHERE path = %s RETURNING id", (path,))
        return cur.fetchone() is not None


_MESSAGE_COLS = ["id", "from_team", "to_team", "subject", "body", "priority",
                 "issue_id", "kind", "status", "draft_response", "reply_to",
                 "read_at", "created_at"]
_MESSAGE_SELECT = ("SELECT id, from_team, to_team, subject, body, priority, "
                   "issue_id, kind, status, draft_response, reply_to, read_at, "
                   "created_at FROM messages")


def create_message(
    pool: ConnectionPool,
    from_team: str,
    to_team: str,
    subject: str,
    body: str = "",
    priority: str = "medium",
    issue_id: Optional[int] = None,
    kind: str = "request",
    status: str = "pending",
    reply_to: Optional[int] = None,
) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO messages (from_team, to_team, subject, body, priority, "
            "issue_id, kind, status, reply_to) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {', '.join(_MESSAGE_COLS)}",
            (from_team, to_team, subject, body, priority, issue_id, kind, status,
             reply_to),
        ).fetchone()
    return dict(zip(_MESSAGE_COLS, row))


def get_message(pool: ConnectionPool, message_id: int) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(_MESSAGE_SELECT + " WHERE id = %s", (message_id,)).fetchone()
    return dict(zip(_MESSAGE_COLS, row)) if row else None


def pending_messages(pool: ConnectionPool, to_team: Optional[str] = None) -> list[dict[str, Any]]:
    """Inbound requests awaiting triage. Responses (kind='response') are never
    ingested — that's what prevents two teams ping-ponging issues forever."""
    where = " WHERE status = 'pending' AND kind = 'request'"
    params: list[Any] = []
    if to_team is not None:
        where += " AND to_team = %s"
        params.append(to_team)
    with pool.connection() as conn:
        rows = conn.execute(_MESSAGE_SELECT + where + " ORDER BY id", params).fetchall()
    return [dict(zip(_MESSAGE_COLS, r)) for r in rows]


def triage_message(pool: ConnectionPool, message_id: int, accept: bool,
                   reason: str = "") -> None:
    """Record the receiving team's triage decision (PROCESS_GUIDE: always theirs)."""
    status = "triaged" if accept else "rejected"
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE messages SET status = %s WHERE id = %s AND status = 'pending' "
            "RETURNING id",
            (status, message_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"message {message_id} not found or not pending")


def archive_message(pool: ConnectionPool, message_id: int) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE messages SET status = 'archived' WHERE id = %s",
                     (message_id,))


def list_responses(pool: ConnectionPool, to_team: Optional[str] = None,
                   unread_only: bool = False) -> list[dict[str, Any]]:
    """Inbound responses (kind='response') addressed to a team — the read side a
    worker needs to consume an answer (comms_read). Newest first. unread_only
    restricts to messages not yet marked read (read_at IS NULL)."""
    where = " WHERE kind = 'response' AND status = 'sent'"
    params: list[Any] = []
    if to_team is not None:
        where += " AND to_team = %s"
        params.append(to_team)
    if unread_only:
        where += " AND read_at IS NULL"
    with pool.connection() as conn:
        rows = conn.execute(
            _MESSAGE_SELECT + where + " ORDER BY id DESC", params).fetchall()
    return [dict(zip(_MESSAGE_COLS, r)) for r in rows]


def mark_message_read(pool: ConnectionPool, message_id: int) -> None:
    """Mark a message consumed so it drops out of the inbox.

    Consuming a message stamps read_at (drops it from my_queue). For an inbound
    *request* still awaiting triage, consuming it IS its terminal disposition, so
    also advance status pending -> archived; otherwise the request stays
    status='pending' forever and keeps resurfacing in comms_check /
    pending_messages (read_at alone does not clear that queue). Responses
    (kind='response', status='sent') are consumed via read_at and are left
    untouched by the status clause. The guard (kind='request' AND
    status='pending') makes this a no-op on already-triaged/archived requests, so
    it never races the engine's auto-triage of team-addressed requests."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE messages SET read_at = now(), "
            "status = CASE WHEN kind = 'request' AND status = 'pending' "
            "THEN 'archived' ELSE status END "
            "WHERE id = %s",
            (message_id,),
        )


def latest_issue_events(
    pool: ConnectionPool, issue_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Most-recent lifecycle event per issue — powers the dashboard 'current work'
    view's 'last major state change' column. Returns {issue_id: {event_type,
    from_state, to_state, created_at, payload}}. Empty in → empty out."""
    if not issue_ids:
        return {}
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (issue_id) issue_id, event_type, from_state, "
            "to_state, created_at, payload FROM issue_events "
            "WHERE issue_id = ANY(%s) ORDER BY issue_id, id DESC",
            (list(issue_ids),),
        ).fetchall()
    return {
        r[0]: {"event_type": r[1], "from_state": r[2], "to_state": r[3],
               "created_at": r[4], "payload": r[5]}
        for r in rows
    }


def list_messages(pool: ConnectionPool, limit: int = 50) -> list[dict[str, Any]]:
    """All messages, newest first — the full correspondence log for the dashboard
    history panel. Every message persists here the moment it's created, so nothing
    is lost (requests, responses, archived all included)."""
    with pool.connection() as conn:
        rows = conn.execute(
            _MESSAGE_SELECT + " ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [dict(zip(_MESSAGE_COLS, r)) for r in rows]


def set_message_draft(pool: ConnectionPool, message_id: int, draft: str) -> None:
    """Cache an agent-suggested reply for human review on the /orch/monitor page."""
    with pool.connection() as conn:
        conn.execute("UPDATE messages SET draft_response = %s WHERE id = %s",
                     (draft, message_id))


def respond_to_message(pool: ConnectionPool, message_id: int, body: str,
                       from_team: str = "orchestration") -> dict[str, Any]:
    """Human-reviewed answer to a queued question: send a response to the asker,
    archive the original out of the queue, and (if the question was linked to an
    issue) drop the answer into that issue's timeline so a resuming worker sees it.
    Mirror of the engine's _comms_respond. Returns the created response message."""
    origin = get_message(pool, message_id)
    if origin is None:
        raise ValueError(f"message {message_id} not found")
    response = create_message(
        pool, from_team=from_team, to_team=origin["from_team"],
        subject=f"Re: {origin['subject']}", body=body,
        priority=origin.get("priority", "medium"),
        issue_id=origin.get("issue_id"), kind="response", status="sent",
        reply_to=message_id,  # thread link: response -> the request it answers
    )
    archive_message(pool, message_id)
    if origin.get("issue_id") is not None:
        append_log(pool, origin["issue_id"], "comms_response_received",
                   {"answer": body, "from_team": from_team,
                    "origin_message_id": message_id})
    return response


# --------------------------------------------------------------------------- #
# Contracts (migration 0011) — one row per endpoint, keyed (method, path).
# The engine gates frontend work on these; backend agrees/registers them via MCP.
# --------------------------------------------------------------------------- #

_CONTRACT_COLS = ["id", "method", "path", "request_ref", "response_dto", "auth",
                  "owner_team", "status", "version", "content_hash", "source_ref",
                  "type_ref", "superseded_by_contract_id", "created_at", "updated_at"]
_CONTRACT_SELECT = "SELECT " + ", ".join(_CONTRACT_COLS) + " FROM contracts"
_SATISFIED = ("agreed", "live")
_CONFIGURED_PROJECT = "cadencelms-working"

# These notification endpoints are intentionally not part of the CadenceLMS
# contract registry.  They are framework/UI notifications rather than the
# product API surface, and importing them made the registry look larger than
# the route/type contract set it is meant to coordinate.
_BULK_IMPORT_EXCLUDED = {
    ("GET", "/notifications"),
    ("PATCH", "/notifications/:id/read"),
    ("POST", "/notifications/read-all"),
    ("GET", "/users/me/notifications"),
    ("GET", "/users/me/notifications/:id"),
    ("PATCH", "/users/me/notifications/:id"),
    ("POST", "/users/me/notifications/mark-all-read"),
    ("GET", "/users/me/notifications/unread-count"),
}


def _contract_hash(method: str, path: str, request_ref: str, response_dto: str) -> str:
    raw = f"{method.upper()}|{path}|{request_ref}|{response_dto}"
    return hashlib.sha256(raw.encode()).hexdigest()


def upsert_contract(
    pool: ConnectionPool,
    method: str,
    path: str,
    request_ref: str = "",
    response_dto: str = "",
    auth: str = "none",
    owner_team: str = "backend",
    status: str = "proposed",
    version: str = "1.0",
    source_ref: Optional[str] = None,
    type_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or fully update a contract (idempotent on method+path). Used by the
    seed import and the contract_upsert MCP tool; recomputes content_hash.
    type_ref points at the contract's type document in packages/contracts."""
    method = method.upper()
    content_hash = _contract_hash(method, path, request_ref, response_dto)
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO contracts (method, path, request_ref, response_dto, auth, "
            "owner_team, status, version, content_hash, source_ref, type_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (method, path) DO UPDATE SET "
            "request_ref = EXCLUDED.request_ref, response_dto = EXCLUDED.response_dto, "
            "auth = EXCLUDED.auth, owner_team = EXCLUDED.owner_team, "
            "status = EXCLUDED.status, version = EXCLUDED.version, "
            "content_hash = EXCLUDED.content_hash, source_ref = EXCLUDED.source_ref, "
            "type_ref = EXCLUDED.type_ref, updated_at = now() "
            f"RETURNING {', '.join(_CONTRACT_COLS)}",
            (method, path, request_ref, response_dto, auth, owner_team, status,
             version, content_hash, source_ref, type_ref),
        ).fetchone()
    return dict(zip(_CONTRACT_COLS, row))


def propose_contract(
    pool: ConnectionPool,
    method: str,
    path: str,
    request_ref: str = "",
    response_dto: str = "",
    owner_team: str = "backend",
    auth: str = "none",
    source_ref: Optional[str] = None,
    type_ref: Optional[str] = None,
    proposed_by: str = "human",
) -> dict[str, Any]:
    """Record a 'proposed' contract a consumer needs. If one already exists it is
    left untouched (never downgrades an agreed/live contract) and returned as-is."""
    method = method.upper()
    content_hash = _contract_hash(method, path, request_ref, response_dto)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO contracts (method, path, request_ref, response_dto, auth, "
            "owner_team, status, content_hash, source_ref, type_ref, proposed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s, %s, %s) "
            "ON CONFLICT (method, path) DO NOTHING",
            (method, path, request_ref, response_dto, auth, owner_team,
             content_hash, source_ref, type_ref, proposed_by),
        )
    return get_contract(pool, method, path)  # type: ignore[return-value]


def recent_contract_proposal_count(pool: ConnectionPool, proposed_by: str,
                                   within_minutes: int = 60) -> int:
    """How many contracts this proposer filed recently — powers the GAP-2 rate
    limit on contract_propose (mirror of recent_adr_proposal_count)."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM contracts WHERE proposed_by = %s "
            "AND created_at > now() - make_interval(mins => %s)",
            (proposed_by, within_minutes),
        ).fetchone()[0]


def set_contract_status(pool: ConnectionPool, method: str, path: str,
                        status: str) -> dict[str, Any]:
    """Move a contract to a new lifecycle status (e.g. agree_contract -> 'agreed')."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE contracts SET status = %s, updated_at = now() "
            "WHERE method = %s AND path = %s "
            f"RETURNING {', '.join(_CONTRACT_COLS)}",
            (status, method.upper(), path),
        ).fetchone()
    if row is None:
        raise ValueError(f"no contract {method.upper()} {path}")
    return dict(zip(_CONTRACT_COLS, row))


def get_contract(pool: ConnectionPool, method: str, path: str) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(
            _CONTRACT_SELECT + " WHERE method = %s AND path = %s",
            (method.upper(), path),
        ).fetchone()
    return dict(zip(_CONTRACT_COLS, row)) if row else None


def list_contracts(pool: ConnectionPool, status: Optional[str] = None,
                   owner_team: Optional[str] = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if owner_team:
        clauses.append("owner_team = %s")
        params.append(owner_team)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with pool.connection() as conn:
        rows = conn.execute(
            _CONTRACT_SELECT + where + " ORDER BY path, method", params).fetchall()
    return [dict(zip(_CONTRACT_COLS, r)) for r in rows]


def contract_satisfied(pool: ConnectionPool, method: str, path: str) -> bool:
    """True if an agreed or live contract exists for this endpoint."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM contracts WHERE method = %s AND path = %s AND status = ANY(%s)",
            (method.upper(), path, list(_SATISFIED)),
        ).fetchone()
    return row is not None


# --- contract proposals (staging layer, migration 0015) -------------------- #

_PROPOSAL_COLS = ["id", "method", "path", "change_type", "request_ref",
                  "response_dto", "auth", "owner_team", "version", "target_status",
                  "content_hash", "source_ref", "type_ref", "status",
                  "created_at", "resolved_at"]
_PROPOSAL_SELECT = "SELECT " + ", ".join(_PROPOSAL_COLS) + " FROM contract_proposals"


def stage_proposal(pool: ConnectionPool, method: str, path: str, change_type: str,
                   request_ref: str = "", response_dto: str = "", auth: str = "none",
                   owner_team: str = "backend", version: str = "1.0",
                   target_status: str = "live",
                   source_ref: Optional[str] = None,
                   type_ref: Optional[str] = None) -> dict[str, Any]:
    """Stage the single pending proposal for an endpoint (replaces any prior
    pending one). The accepted `contracts` row is untouched until accept_proposal."""
    method = method.upper()
    content_hash = _contract_hash(method, path, request_ref, response_dto)
    with pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM contract_proposals WHERE method=%s AND path=%s "
                     "AND status='pending'", (method, path))
        row = conn.execute(
            "INSERT INTO contract_proposals (method, path, change_type, request_ref, "
            "response_dto, auth, owner_team, version, target_status, content_hash, "
            "source_ref, type_ref) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"RETURNING {', '.join(_PROPOSAL_COLS)}",
            (method, path, change_type, request_ref, response_dto, auth, owner_team,
             version, target_status, content_hash, source_ref, type_ref),
        ).fetchone()
    return dict(zip(_PROPOSAL_COLS, row))


def list_proposals(pool: ConnectionPool, status: str = "pending") -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(_PROPOSAL_SELECT + " WHERE status = %s ORDER BY path, method",
                            (status,)).fetchall()
    return [dict(zip(_PROPOSAL_COLS, r)) for r in rows]


def get_proposal(pool: ConnectionPool, method: str, path: str,
                 status: str = "pending") -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(
            _PROPOSAL_SELECT + " WHERE method=%s AND path=%s AND status=%s "
            "ORDER BY id DESC LIMIT 1", (method.upper(), path, status)).fetchone()
    return dict(zip(_PROPOSAL_COLS, row)) if row else None


def stage_from_seed(pool: ConnectionPool, rows: list[dict[str, Any]],
                    full: bool = True) -> dict[str, int]:
    """Diff a seed against the accepted store and stage proposals. add = no
    contract; modify = accepted contract whose hash differs (drift); skip = same;
    remove (full only) = accepted contract absent from the seed. Idempotent."""
    counts = {"add": 0, "modify": 0, "remove": 0, "skip": 0}
    if full:
        with pool.connection() as conn:
            conn.execute("DELETE FROM contract_proposals WHERE status='pending'")
    seen: set[tuple[str, str]] = set()
    for r in rows:
        method, path = r["method"].upper(), r["path"]
        seen.add((method, path))
        existing = get_contract(pool, method, path)
        h = _contract_hash(method, path, r.get("request_ref", ""), r.get("response_dto", ""))
        if existing and existing["status"] in _SATISFIED:
            if existing["content_hash"] == h:
                counts["skip"] += 1
                continue
            ctype = "modify"
        else:
            ctype = "add"
        stage_proposal(pool, method, path, ctype,
                       request_ref=r.get("request_ref", ""),
                       response_dto=r.get("response_dto", ""),
                       auth=r.get("auth", "none"),
                       owner_team=r.get("owner_team", "backend"),
                       version=str(r.get("version", "1.0")),
                       target_status=r.get("status", "live"),
                       source_ref=r.get("source_ref"),
                       type_ref=r.get("type_ref"))
        counts[ctype] += 1
    if full:
        for c in list_contracts(pool):
            if c["status"] in _SATISFIED and (c["method"], c["path"]) not in seen:
                stage_proposal(pool, c["method"], c["path"], "remove",
                               request_ref=c["request_ref"], response_dto=c["response_dto"],
                               auth=c["auth"], owner_team=c["owner_team"],
                               version=c["version"], source_ref=c["source_ref"],
                               type_ref=c.get("type_ref"))
                counts["remove"] += 1
    return counts


def bulk_rebuild_contracts(pool: ConnectionPool, rows: list[dict[str, Any]],
                           repository_name: str,
                           expected_repository: str = "cadencelms-working",
                           source_ref: Optional[str] = None) -> dict[str, Any]:
    """Reconcile the accepted contract store from a complete seed in one admin
    operation.

    This is deliberately separate from ``contract_propose``: it is a bounded,
    idempotent administrative import, not a way for workers to evade proposal
    limits.  The dashboard supplies the human confirmation string, but this
    function repeats the exact server-side check so callers cannot accidentally
    turn the action into an unrestricted import endpoint.

    Existing ``agreed``/``live`` status is preserved.  New and changed shapes
    become ``agreed`` because this action is the explicitly approved admin
    rebuild requested from the Contracts page.  Full-seed removals are marked
    deprecated.  Invalid or duplicate endpoint keys are reported as conflicts
    before any database work starts.
    """
    if (repository_name or "").strip() != expected_repository:
        raise ValueError(f"type {expected_repository} exactly to confirm this admin import")
    if not isinstance(rows, list):
        raise ValueError("contract seed must be a JSON array")

    normalized: list[dict[str, Any]] = []
    conflicts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            conflicts.append(f"row {i + 1}: expected an object")
            continue
        method = str(raw.get("method", "")).strip().upper()
        path = str(raw.get("path", "")).strip()
        key = (method, path)
        if not method or not path or not path.startswith("/"):
            conflicts.append(f"row {i + 1}: invalid method/path")
            continue
        if key in _BULK_IMPORT_EXCLUDED:
            continue
        if key in seen:
            conflicts.append(f"duplicate endpoint: {method} {path}")
            continue
        seen.add(key)
        item = dict(raw)
        item["method"], item["path"] = method, path
        if source_ref and not item.get("source_ref"):
            item["source_ref"] = source_ref
        normalized.append(item)
    if conflicts:
        return {"imported": 0, "agreed": 0, "skipped": 0, "deprecated": 0,
                "conflicts": conflicts, "excluded": len(rows) - len(normalized)}

    # stage_from_seed is idempotent and performs the complete-set diff.  Capture
    # statuses before accepting proposals so a live contract is never downgraded.
    before = {(c["method"], c["path"]): c for c in list_contracts(pool)}
    staged = stage_from_seed(pool, normalized, full=True)
    agreed = imported = deprecated = 0
    for proposal in list_proposals(pool, "pending"):
        key = (proposal["method"], proposal["path"])
        if proposal["change_type"] == "remove":
            accept_proposal(pool, *key)
            deprecated += 1
            continue
        prior = before.get(key)
        target = prior["status"] if prior and prior["status"] in _SATISFIED else "agreed"
        accept_proposal(pool, *key, status=target)
        imported += 1
        agreed += 1
    return {"imported": imported, "agreed": agreed, "skipped": staged["skip"],
            "deprecated": deprecated, "conflicts": [],
            "excluded": len(rows) - len(normalized), "staged": staged}


def _contract_audit_repo_root(repo_path: str | Path) -> Path:
    """Resolve the repository root used by the current-contract audit."""
    root = Path(repo_path).expanduser().resolve()
    candidates = (root, root / "wt-backend-dev", root.parent / "wt-backend-dev", root.parent)
    # A configured monorepo checkout may have an older contracts directory while
    # the active backend worktree owns the current audit. Prefer an existing
    # audit artifact before falling back to the first structurally valid tree.
    candidates = tuple(dict.fromkeys(candidates))
    candidates = tuple(sorted(
        candidates,
        key=lambda candidate: not (candidate / "packages/contracts/contracts.audit.json").is_file(),
    ))
    for candidate in candidates:
        if (candidate / "apps/api/src/routes").is_dir() and (candidate / "packages/contracts").is_dir():
            return candidate
    raise ValueError(f"no CadenceLMS API/contracts tree found under {root}")


def resolve_contract_audit_repo(repo_path: str | Path) -> Path:
    """Public resolver for dashboard/actions that operate on the audit artifact."""
    return _contract_audit_repo_root(repo_path)


def _parse_current_route_file(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Extract concrete Express route declarations without executing API code.

    The audit intentionally records implementation evidence, not runtime traffic.
    This parser follows the same route declaration convention as the API's
    divergence analyzer: router.<verb>("/path", ...).
    """
    source = path.read_text(encoding="utf-8")
    file_auth = bool(re.search(r"router\.use\(\s*authenticate\b", source))
    out: list[dict[str, Any]] = []
    pattern = re.compile(r"router\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]")
    for match in pattern.finditer(source):
        method, route_path = match.group(1).upper(), match.group(2)
        line = source.count("\n", 0, match.start()) + 1
        # Inspect only this declaration's line window for middleware evidence;
        # file-level auth covers the common router.use(authenticate) pattern.
        window = source[match.start(): source.find("\n", match.start()) if source.find("\n", match.start()) >= 0 else len(source)]
        validate = re.search(r"validateRequest\s*\([^)]*\b(body|query|params)\s*:\s*([A-Za-z0-9_.]+)", window)
        request_ref = validate.group(2) if validate else ""
        authenticated = file_auth or bool(re.search(r"\bauthenticate\b|\bauthorize\s*\(", window))
        out.append({
            "method": method,
            "path": route_path,
            "request_ref": request_ref,
            "response_dto": "",
            "auth": "jwt" if authenticated else "none",
            "owner_team": "backend",
            "status": "live",
            "version": "1.0",
            "source_ref": f"{path.relative_to(repo_root)}:{line}",
            "type_ref": "",
        })
    return out


def refresh_contract_audit(repo_path: str | Path) -> dict[str, Any]:
    """Build and persist the current implementation contract snapshot.

    Output is a predictable, reviewable JSON object at
    ``packages/contracts/contracts.audit.json``.  The file contains metadata,
    endpoint rows, and explicit coverage gaps.  It is deliberately separate
    from the historical ``contracts.seed.json`` and does not mutate the
    orchestrator contract store.
    """
    root = _contract_audit_repo_root(repo_path)
    routes_dir = root / "apps/api/src/routes"
    audit_path = root / "packages/contracts/contracts.audit.json"
    previous: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in (audit_path, root / "packages/contracts/contracts.seed.json"):
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            prior_rows = raw.get("contracts", []) if isinstance(raw, dict) else raw
            if isinstance(prior_rows, list):
                previous = {(str(r.get("method", "")).upper(), str(r.get("path", ""))): r
                            for r in prior_rows if isinstance(r, dict)}
                break
        except (OSError, json.JSONDecodeError):
            continue

    rows: list[dict[str, Any]] = []
    for route_file in sorted(routes_dir.glob("*.routes.ts")):
        rows.extend(_parse_current_route_file(route_file, root))
    seen: set[tuple[str, str]] = set()
    conflicts: list[str] = []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        key = (row["method"], row["path"])
        if key in seen:
            conflicts.append(f"duplicate endpoint: {key[0]} {key[1]}")
            continue
        seen.add(key)
        old = previous.get(key, {})
        # Keep evidence that remains useful across a route-source refresh, but
        # always replace implementation location/auth/request evidence from code.
        row["type_ref"] = old.get("type_ref", "") or ""
        row["response_dto"] = old.get("response_dto", "") or ""
        normalized.append(row)
    normalized.sort(key=lambda r: (r["path"], r["method"]))
    unresolved_response = sum(1 for r in normalized if not r["response_dto"])
    unresolved_type = sum(1 for r in normalized if not r["type_ref"])
    payload = {
        "schema_version": "1.0",
        "kind": "cadencelms.contracts.current-audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": root.name,
        "source": {
            "routes": "apps/api/src/routes/*.routes.ts",
            "validators": "apps/api/src/validators/*.ts",
            "dto_inventory": "apps/api/src/dto/**/*.ts",
            "shared_types": "packages/contracts/types/*.ts",
        },
        "summary": {
            "implemented_routes": len(normalized),
            "unique_endpoint_keys": len(seen),
            "duplicate_endpoint_keys": len(conflicts),
            "unresolved_response_dto": unresolved_response,
            "unresolved_type_ref": unresolved_type,
        },
        "gaps": {
            "duplicate_endpoints": conflicts,
            "response_dto_mapping": "Controller response mapping requires explicit evidence; blank means unresolved.",
            "type_ref_mapping": "Blank type_ref means no prior shared-type mapping was available.",
        },
        "contracts": normalized,
    }
    audit_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"path": str(audit_path), "root": str(root), "rows": normalized,
            "summary": payload["summary"], "gaps": payload["gaps"]}


def load_contract_audit(repo_path: str | Path) -> dict[str, Any]:
    """Load the last generated audit file, refusing ambiguous bare seed arrays."""
    root = _contract_audit_repo_root(repo_path)
    path = root / "packages/contracts/contracts.audit.json"
    if not path.is_file():
        raise ValueError(f"no current contract audit file found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read contract audit file: {exc}") from exc
    rows = payload.get("contracts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("contract audit file must contain a contracts array")
    return {"path": str(path), "root": str(root), "rows": rows,
            "summary": payload.get("summary", {}), "gaps": payload.get("gaps", {})}


def accept_proposal(pool: ConnectionPool, method: str, path: str,
                    status: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Apply the pending proposal to the accepted store: add/modify upserts the
    contract (status = `status` override, else the proposal's target_status, e.g.
    agreed/live) so the gate is satisfied; remove deprecates it. Marks the proposal
    accepted. Returns the contract."""
    p = get_proposal(pool, method, path)
    if p is None:
        raise ValueError(f"no pending proposal for {method.upper()} {path}")
    if p["change_type"] == "remove":
        contract = set_contract_status(pool, method, path, "deprecated")
    else:
        contract = upsert_contract(
            pool, method, path, request_ref=p["request_ref"],
            response_dto=p["response_dto"], auth=p["auth"], owner_team=p["owner_team"],
            status=status or p["target_status"], version=p["version"],
            source_ref=p["source_ref"], type_ref=p.get("type_ref"))
    with pool.connection() as conn:
        conn.execute("UPDATE contract_proposals SET status='accepted', resolved_at=now() "
                     "WHERE id=%s", (p["id"],))
    return contract


def accept_contract_review(pool: ConnectionPool, method: str, path: str,
                           status: Optional[str] = None) -> dict[str, Any]:
    """Accept the contract row currently visible on the /contracts page.

    New contract imports land in ``contract_proposals`` and are accepted via
    ``accept_proposal``. Older/agent-created needs may already exist directly in
    ``contracts`` with status ``proposed`` and no proposal row. The dashboard
    renders both forms of pending review, so its Accept action must handle both.
    Direct proposed rows become ``agreed`` by default: the shape is accepted and
    frontend gates unblock, without claiming the endpoint is already live.
    """
    p = get_proposal(pool, method, path)
    if p is not None:
        contract = accept_proposal(pool, method, path, status=status)
        if contract is None:
            raise ValueError(f"no contract produced for {method.upper()} {path}")
        return contract

    contract = get_contract(pool, method, path)
    if contract is None:
        raise ValueError(f"no contract {method.upper()} {path}")
    if contract["status"] != "proposed":
        raise ValueError(f"no pending proposal for {method.upper()} {path}")
    return set_contract_status(pool, method, path, status or "agreed")


def reject_proposal(pool: ConnectionPool, method: str, path: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE contract_proposals SET status='rejected', resolved_at=now() "
                     "WHERE method=%s AND path=%s AND status='pending'",
                     (method.upper(), path))


def consumers_of(pool: ConnectionPool, method: str, path: str) -> list[str]:
    """Teams whose issues consume this endpoint (from issue_contract_deps) — the
    affected consumers for change/removal work."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT i.team FROM issue_contract_deps d JOIN issues i "
            "ON i.id = d.issue_id WHERE d.method=%s AND d.path=%s",
            (method.upper(), path)).fetchall()
    return [r[0] for r in rows]


def _contract_state(contract: Optional[dict], proposal: Optional[dict]) -> str:
    if proposal is not None:
        return {"add": "awaiting acceptance", "modify": "drifted",
                "remove": "removal pending"}.get(proposal["change_type"], "pending")
    if contract is None:
        return "missing"
    if contract["status"] in _SATISFIED:
        return "up-to-date"
    if contract["status"] in ("superseded", "retired", "rejected"):
        return contract["status"]
    if contract["status"] == "deprecated":
        return "deprecated"
    return "awaiting acceptance"


def contracts_overview(pool: ConnectionPool) -> list[dict[str, Any]]:
    """Per-endpoint {contract, proposal, state} for the /contracts page — the union
    of accepted contracts and pending proposals."""
    contracts = {(c["method"], c["path"]): c for c in list_contracts(pool)}
    proposals = {(p["method"], p["path"]): p for p in list_proposals(pool)}
    out = []
    for key in sorted(set(contracts) | set(proposals), key=lambda k: (k[1], k[0])):
        c, p = contracts.get(key), proposals.get(key)
        out.append({"method": key[0], "path": key[1], "contract": c, "proposal": p,
                    "state": _contract_state(c, p)})
    return out


def add_issue_contract_deps(pool: ConnectionPool, issue_id: int,
                            deps: list[dict[str, str]]) -> None:
    """Record the endpoints a blocked issue is waiting on (contract_check)."""
    if not deps:
        return
    with pool.connection() as conn, conn.transaction():
        for d in deps:
            conn.execute(
                "INSERT INTO issue_contract_deps (issue_id, method, path) "
                "VALUES (%s, %s, %s)",
                (issue_id, d["method"].upper(), d["path"]),
            )


def list_issue_contract_deps(pool: ConnectionPool, issue_id: int) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, issue_id, method, path, satisfied, created_at "
            "FROM issue_contract_deps WHERE issue_id = %s ORDER BY id",
            (issue_id,),
        ).fetchall()
    cols = ["id", "issue_id", "method", "path", "satisfied", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def mark_contract_deps_satisfied(pool: ConnectionPool, issue_id: int) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE issue_contract_deps SET satisfied = TRUE WHERE issue_id = %s",
            (issue_id,))


# --- contract lifecycle (migration 0023, admin-driven reconciliation) -------- #

_LIFECYCLE_OP_COLS = ["id", "project", "operation_id", "actor", "actor_role",
                      "reason", "source", "result", "requested", "response",
                      "created_at"]
_LIFECYCLE_EVENT_COLS = ["id", "op_id", "contract_id", "method", "path", "action",
                         "from_status", "to_status", "superseded_by_contract_id",
                         "reason", "actor", "source_ref", "content_hash", "created_at"]


def _activating_route_drift(pool: ConnectionPool, settings, changes, accept: bool):
    """Canonical-route gate (step 8b) for preview/apply.

    A contract moved INTO a satisfying state (agree/reinstate) must have a
    backing route in the code scan; otherwise we'd agree an endpoint that does
    not exist. Superseding/retiring/deprecating is never gated (pre-existing
    drift on a contract you are retiring is exactly what you are resolving).

    Opt-in: ``settings is None`` -> no-op (unchanged behaviour, hermetic tests).
    Fail-safe: a missing/unreadable product tree yields no findings. Returns
    ``(conflicts, warnings)``; when ``accept`` is True the findings are demoted
    to warnings so an administrator can proceed deliberately.
    """
    if settings is None:
        return [], []
    from . import contract_lifecycle as lifecycle
    from . import contract_drift
    activating = [c["contract_id"] for c in changes
                  if isinstance(c, dict) and isinstance(c.get("contract_id"), int)
                  and lifecycle.ACTION_TO_STATUS.get(c.get("action")) in _SATISFIED]
    if not activating:
        return [], []
    drift = contract_drift.drift_for_contracts(pool, settings, activating)
    msgs = [f"route check: contract #{f['contract_id']} {f['method']} {f['path']} "
            "has no backing route (unbacked)"
            for f in drift["blocking"] + drift["advisory"]
            if f.get("category") == "unbacked_contract"]
    if not msgs:
        return [], []
    return ([], msgs) if accept else (msgs, [])


def contract_lifecycle_preview(
    pool: ConnectionPool,
    project: str,
    operation_id: str,
    actor: str,
    actor_role: str,
    reason: str,
    changes: list[dict[str, Any]],
    expected: Optional[dict[str, str]] = None,
    expected_project: str = _CONFIGURED_PROJECT,
    settings=None,
    accept_route_drift: bool = False,
) -> dict[str, Any]:
    """Read-only. Loads the affected contracts, validates via
    contract_lifecycle.validate_batch, classifies destructive changes, and
    reports concurrency tokens. NO writes, no transaction that mutates.

    When ``settings`` is supplied, also runs the canonical-route gate (8b):
    contracts being activated must have a backing route or the batch is invalid
    (see _activating_route_drift). Opt-in + fail-safe."""
    from . import contract_lifecycle as lifecycle

    conflicts = []

    # Project passthrough validation.
    if project.strip() != expected_project:
        conflicts.append(f"project '{project}' does not match configured project")

    # Load all referenced contracts (both contract_id and replacement_contract_id).
    contract_ids = set()
    for change in changes:
        if isinstance(change, dict):
            cid = change.get("contract_id")
            if isinstance(cid, int):
                contract_ids.add(cid)
            rid = change.get("replacement_contract_id")
            if isinstance(rid, int):
                contract_ids.add(rid)

    contracts_by_id: dict[int, dict[str, Any]] = {}
    if contract_ids:
        with pool.connection() as conn:
            rows = conn.execute(
                _CONTRACT_SELECT + " WHERE id = ANY(%s) ORDER BY id",
                (list(contract_ids),),
            ).fetchall()
        contracts_by_id = {
            row[0]: dict(zip(_CONTRACT_COLS, row))
            for row in rows
        }

    # Validate the batch.
    normalized, batch_conflicts, warnings = lifecycle.validate_batch(
        changes, contracts_by_id, project)
    conflicts.extend(batch_conflicts)

    # Canonical-route gate (8b): opt-in, fail-safe.
    route_conflicts, route_warnings = _activating_route_drift(
        pool, settings, changes, accept_route_drift)
    conflicts.extend(route_conflicts)
    warnings = list(warnings) + route_warnings

    # Classify destructive changes.
    destructive = any(c["destructive"] for c in normalized)

    # Build affected list with concurrency tokens.
    affected = []
    for change in normalized:
        contract = contracts_by_id.get(change["contract_id"], {})
        affected.append({
            "contract_id": change["contract_id"],
            "method": change["method"],
            "path": change["path"],
            "from_status": change["from_status"],
            "to_status": change["to_status"],
            "action": change["action"],
            "destructive": change["destructive"],
            "token": contract.get("updated_at", "").isoformat()
                if isinstance(contract.get("updated_at"), datetime) else
                (contract.get("updated_at") or ""),
        })

    # Check for token staleness (advisory).
    token_stale_issues = []
    if expected:
        for change in normalized:
            contract = contracts_by_id.get(change["contract_id"], {})
            exp_token = expected.get(str(change["contract_id"]))
            live_token = contract.get("updated_at", "").isoformat() \
                if isinstance(contract.get("updated_at"), datetime) else \
                (contract.get("updated_at") or "")
            if exp_token and live_token and exp_token != live_token:
                token_stale_issues.append(change["contract_id"])

    # Compute preview_token (digest over operation_id, normalized, tokens).
    import hashlib
    preview_data = (
        operation_id +
        json.dumps(normalized, default=str, sort_keys=True) +
        json.dumps([a["token"] for a in affected], sort_keys=True)
    )
    preview_token = hashlib.sha256(preview_data.encode()).hexdigest()

    return {
        "valid": not conflicts,
        "project": project,
        "operation_id": operation_id,
        "normalized_changes": normalized,
        "conflicts": conflicts,
        "warnings": warnings,
        "affected": affected,
        "destructive": destructive,
        "confirmation_required": destructive,
        "preview_token": preview_token,
    }


def contract_lifecycle_apply(
    pool: ConnectionPool,
    project: str,
    operation_id: str,
    actor: str,
    actor_role: str,
    reason: str,
    changes: list[dict[str, Any]],
    expected: Optional[dict[str, str]] = None,
    source: str = "",
    confirm_project: Optional[str] = None,
    expected_project: str = _CONFIGURED_PROJECT,
    settings=None,
    accept_route_drift: bool = False,
) -> dict[str, Any]:
    """Atomic, idempotent admin apply of a lifecycle batch. ONE
    with pool.connection() as conn, conn.transaction(): block.

    When ``settings`` is supplied, the canonical-route gate (8b) runs first
    (outside the transaction, so no filesystem I/O is done under the row locks):
    activating a contract whose route has no backing implementation is rejected
    as a conflict unless ``accept_route_drift`` is set. Opt-in + fail-safe."""
    from . import contract_lifecycle as lifecycle

    # Canonical-route gate (8b) — computed before the transaction (fail-safe).
    route_conflicts, route_warnings = _activating_route_drift(
        pool, settings, changes, accept_route_drift)

    with pool.connection() as conn, conn.transaction():
        # 1. Idempotency replay (first, before any lock/validation).
        existing = conn.execute(
            "SELECT response FROM contract_lifecycle_ops "
            "WHERE project=%s AND operation_id=%s",
            (project, operation_id),
        ).fetchone()
        if existing is not None:
            return existing[0]

        # 2. Load + lock all referenced contracts.
        contract_ids = set()
        for change in changes:
            if isinstance(change, dict):
                cid = change.get("contract_id")
                if isinstance(cid, int):
                    contract_ids.add(cid)
                rid = change.get("replacement_contract_id")
                if isinstance(rid, int):
                    contract_ids.add(rid)

        contracts_by_id: dict[int, dict[str, Any]] = {}
        if contract_ids:
            rows = conn.execute(
                _CONTRACT_SELECT + " WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (list(contract_ids),),
            ).fetchall()
            contracts_by_id = {
                row[0]: dict(zip(_CONTRACT_COLS, row))
                for row in rows
            }

        # 3. Project passthrough gate.
        if project.strip() != expected_project:
            response = {
                "result": "rejected",
                "reason": "project_mismatch",
                "project": project,
                "operation_id": operation_id,
            }
            op_id = conn.execute(
                "INSERT INTO contract_lifecycle_ops "
                "(project, operation_id, actor, actor_role, reason, source, "
                "result, requested, response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (project, operation_id, actor, actor_role, reason, source,
                 "rejected", Jsonb(changes), Jsonb(response)),
            ).fetchone()[0]
            response["audit_op_id"] = op_id
            return response

        # 4. Validate under lock (plus the 8b canonical-route gate).
        normalized, conflicts, warnings = lifecycle.validate_batch(
            changes, contracts_by_id, project)
        conflicts = list(conflicts) + route_conflicts
        warnings = list(warnings) + route_warnings
        if conflicts:
            response = {
                "result": "conflict",
                "reason": "validation_failed",
                "operation_id": operation_id,
                "conflicts": conflicts,
                "warnings": warnings,
            }
            op_id = conn.execute(
                "INSERT INTO contract_lifecycle_ops "
                "(project, operation_id, actor, actor_role, reason, source, "
                "result, requested, response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (project, operation_id, actor, actor_role, reason, source,
                 "conflict", Jsonb(changes), Jsonb(response)),
            ).fetchone()[0]
            response["audit_op_id"] = op_id
            return response

        # 5. Destructive confirmation gate.
        destructive = any(c["destructive"] for c in normalized)
        if destructive and (confirm_project or "").strip() != expected_project:
            response = {
                "result": "rejected",
                "reason": "confirmation_required",
                "operation_id": operation_id,
                "destructive_changes": [c["contract_id"] for c in normalized
                                       if c["destructive"]],
            }
            op_id = conn.execute(
                "INSERT INTO contract_lifecycle_ops "
                "(project, operation_id, actor, actor_role, reason, source, "
                "result, requested, response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (project, operation_id, actor, actor_role, reason, source,
                 "rejected", Jsonb(changes), Jsonb(response)),
            ).fetchone()[0]
            response["audit_op_id"] = op_id
            return response

        # 6. Concurrency re-check.
        if expected:
            stale = []
            for change in normalized:
                contract = contracts_by_id.get(change["contract_id"], {})
                exp_token = expected.get(str(change["contract_id"]))
                live_token = contract.get("updated_at").isoformat() \
                    if isinstance(contract.get("updated_at"), datetime) else \
                    (contract.get("updated_at") or "")
                if exp_token and live_token and exp_token != live_token:
                    stale.append(change["contract_id"])
            if stale:
                response = {
                    "result": "conflict",
                    "reason": "stale_token",
                    "operation_id": operation_id,
                    "stale": stale,
                }
                op_id = conn.execute(
                    "INSERT INTO contract_lifecycle_ops "
                    "(project, operation_id, actor, actor_role, reason, source, "
                    "result, requested, response) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (project, operation_id, actor, actor_role, reason, source,
                     "conflict", Jsonb(changes), Jsonb(response)),
                ).fetchone()[0]
                response["audit_op_id"] = op_id
                return response

        # 7&8. Apply each change, then insert ops row with final response.
        # Insert the ops row first to get its id, then insert events.
        op_row = conn.execute(
            "INSERT INTO contract_lifecycle_ops "
            "(project, operation_id, actor, actor_role, reason, source, "
            "result, requested, response) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (project, operation_id, actor, actor_role, reason, source,
             "applied", Jsonb(normalized), Jsonb({})),
        ).fetchone()
        op_id = op_row[0]

        changed = []
        for change in normalized:
            contract = contracts_by_id.get(change["contract_id"], {})

            # Update the contract.
            conn.execute(
                "UPDATE contracts SET status=%s, superseded_by_contract_id=%s, updated_at=now() "
                "WHERE id=%s",
                (change["to_status"],
                 change.get("replacement_contract_id"),
                 change["contract_id"]),
            )

            # Insert event.
            conn.execute(
                "INSERT INTO contract_lifecycle_events "
                "(op_id, contract_id, method, path, action, from_status, to_status, "
                "superseded_by_contract_id, reason, actor, source_ref, content_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (op_id, change["contract_id"], change["method"], change["path"],
                 change["action"], change["from_status"], change["to_status"],
                 change.get("replacement_contract_id"),
                 reason, actor, change.get("source_ref"), change.get("content_hash")),
            )

            changed.append({
                "contract_id": change["contract_id"],
                "method": change["method"],
                "path": change["path"],
                "action": change["action"],
                "from_status": change["from_status"],
                "to_status": change["to_status"],
                "superseded_by_contract_id": change.get("replacement_contract_id"),
            })

        # Build final response.
        response = {
            "result": "applied",
            "operation_id": operation_id,
            "project": project,
            "audit_op_id": op_id,
            "changed": changed,
            "unchanged": [],
            "warnings": warnings,
            "destructive": destructive,
            "confirmed": {
                "required": destructive,
                "value_matched": destructive and
                    (confirm_project or "").strip() == expected_project,
            },
        }

        # Update the ops row with the final response.
        conn.execute(
            "UPDATE contract_lifecycle_ops SET response=%s WHERE id=%s",
            (Jsonb(response), op_id),
        )

    return response


def contract_lifecycle_history(
    pool: ConnectionPool,
    contract_id: Optional[int] = None,
    operation_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read-only. Return append-only lifecycle events joined to their ops row for
    actor/reason/source/project. Filter by contract_id and/or operation_id
    (operation_id here means the TEXT op key; join ops on op.operation_id).
    Ordered by e.created_at DESC, e.id DESC."""
    with pool.connection() as conn:
        clauses = []
        params = []
        if contract_id is not None:
            clauses.append("e.contract_id = %s")
            params.append(contract_id)
        if operation_id is not None:
            clauses.append("o.operation_id = %s")
            params.append(operation_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        rows = conn.execute(
            "SELECT e.id, e.op_id, e.contract_id, e.method, e.path, e.action, "
            "e.from_status, e.to_status, e.superseded_by_contract_id, e.reason, "
            "e.actor, e.source_ref, e.content_hash, e.created_at, "
            "o.actor, o.reason, o.source, o.project, o.operation_id, o.result "
            "FROM contract_lifecycle_events e "
            "JOIN contract_lifecycle_ops o ON o.id = e.op_id " +
            where +
            " ORDER BY e.created_at DESC, e.id DESC",
            params,
        ).fetchall()

    # Build result dicts. Event cols first, then joined op fields.
    event_cols = ["id", "op_id", "contract_id", "method", "path", "action",
                  "from_status", "to_status", "superseded_by_contract_id",
                  "reason", "actor", "source_ref", "content_hash", "created_at"]
    op_cols = ["actor", "reason", "source", "project", "operation_id", "result"]

    result = []
    for row in rows:
        d = dict(zip(event_cols + op_cols, row))
        result.append(d)

    return result


_AMENDABLE_FIELDS = frozenset(
    {"auth", "type_ref", "request_ref", "response_dto", "source_ref", "path"})


def amend_contract_metadata(
    pool: ConnectionPool,
    project: str,
    operation_id: str,
    actor: str,
    actor_role: str,
    reason: str,
    amendments: list[dict[str, Any]],
    source: str = "",
    expected_project: str = _CONFIGURED_PROJECT,
) -> dict[str, Any]:
    """Audited, idempotent correction of contract METADATA — no lifecycle
    transition (status is unchanged).

    Amends only the fields in ``_AMENDABLE_FIELDS`` (auth/type_ref/request_ref/
    response_dto/source_ref/path). ``content_hash`` is recomputed whenever a hash
    input (path/request_ref/response_dto) changes. ``path`` is part of
    UNIQUE(method,path); a path change that would collide with another contract
    is rejected. Recorded in the same contract_lifecycle_ops/events audit tables
    (result='amended', action='amend', from_status==to_status). Idempotent on
    (project, operation_id): a replay returns the original response.
    """
    with pool.connection() as conn, conn.transaction():
        # Idempotency replay.
        existing = conn.execute(
            "SELECT response FROM contract_lifecycle_ops "
            "WHERE project=%s AND operation_id=%s",
            (project, operation_id),
        ).fetchone()
        if existing is not None:
            return existing[0]

        def _record(result: str, response: dict[str, Any]) -> int:
            return conn.execute(
                "INSERT INTO contract_lifecycle_ops "
                "(project, operation_id, actor, actor_role, reason, source, "
                "result, requested, response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (project, operation_id, actor, actor_role, reason, source,
                 result, Jsonb(amendments), Jsonb(response)),
            ).fetchone()[0]

        # Project passthrough gate.
        if project.strip() != expected_project:
            response = {"result": "rejected", "reason": "project_mismatch",
                        "project": project, "operation_id": operation_id}
            response["audit_op_id"] = _record("rejected", response)
            return response

        ids = [a["contract_id"] for a in amendments
               if isinstance(a, dict) and isinstance(a.get("contract_id"), int)]
        rows = conn.execute(
            _CONTRACT_SELECT + " WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids,),
        ).fetchall()
        by_id = {r[0]: dict(zip(_CONTRACT_COLS, r)) for r in rows}

        # Validate.
        conflicts: list[str] = []
        for a in amendments:
            cid = a.get("contract_id")
            if cid not in by_id:
                conflicts.append(f"unknown contract id {cid}")
                continue
            bad = set(a) - {"contract_id"} - _AMENDABLE_FIELDS
            if bad:
                conflicts.append(f"contract {cid}: non-amendable fields {sorted(bad)}")
            if "path" in a:
                method = by_id[cid]["method"]
                clash = conn.execute(
                    "SELECT id FROM contracts WHERE method=%s AND path=%s AND id<>%s",
                    (method, a["path"], cid),
                ).fetchone()
                if clash:
                    conflicts.append(
                        f"contract {cid}: path {method} {a['path']} already used by "
                        f"contract {clash[0]}")
        if conflicts:
            response = {"result": "conflict", "reason": "validation_failed",
                        "operation_id": operation_id, "conflicts": conflicts}
            response["audit_op_id"] = _record("conflict", response)
            return response

        # Apply.
        op_id = _record("amended", {})
        changed = []
        for a in amendments:
            cid = a["contract_id"]
            c = by_id[cid]
            updates = {k: a[k] for k in _AMENDABLE_FIELDS if k in a}
            new_path = updates.get("path", c["path"])
            new_req = updates.get("request_ref", c["request_ref"])
            new_dto = updates.get("response_dto", c["response_dto"])
            new_hash = _contract_hash(c["method"], new_path, new_req, new_dto)

            set_cols, vals = [], []
            for k, v in updates.items():
                set_cols.append(f"{k} = %s")
                vals.append(v)
            set_cols.append("content_hash = %s")
            vals.append(new_hash)
            set_cols.append("updated_at = now()")
            vals.append(cid)
            conn.execute(
                f"UPDATE contracts SET {', '.join(set_cols)} WHERE id = %s", vals)

            conn.execute(
                "INSERT INTO contract_lifecycle_events "
                "(op_id, contract_id, method, path, action, from_status, to_status, "
                "superseded_by_contract_id, reason, actor, source_ref, content_hash) "
                "VALUES (%s, %s, %s, %s, 'amend', %s, %s, %s, %s, %s, %s, %s)",
                (op_id, cid, c["method"], new_path, c["status"], c["status"],
                 c["superseded_by_contract_id"], reason, actor,
                 updates.get("source_ref", c["source_ref"]), new_hash),
            )
            changed.append({"contract_id": cid, "fields": sorted(updates),
                            "path": new_path, "content_hash": new_hash})

        response = {"result": "amended", "operation_id": operation_id,
                    "project": project, "audit_op_id": op_id, "changed": changed}
        conn.execute(
            "UPDATE contract_lifecycle_ops SET response=%s WHERE id=%s",
            (Jsonb(response), op_id))

    return response


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

_ISSUE_SELECT = (
    "SELECT id, goal_id, title, description, parent_id, depth, team, pipeline, "
    "state, gate_type, retry_count, step_count, assigned_agent, "
    "triggered_by_message, origin_message_id, work_type, created_at, updated_at FROM issues"
)


def _issue_from_row(row: tuple) -> Issue:
    return Issue(
        id=row[0], goal_id=row[1], title=row[2], description=row[3],
        parent_id=row[4], depth=row[5], team=row[6], pipeline=row[7],
        state=row[8], gate_type=row[9], retry_count=row[10], step_count=row[11],
        assigned_agent=row[12], triggered_by_message=row[13],
        origin_message_id=row[14], work_type=row[15],
        created_at=row[16], updated_at=row[17],
    )


def _append_event(
    conn,
    issue_id: int,
    event_type: str,
    from_state: Optional[str],
    to_state: Optional[str],
    payload: dict[str, Any],
) -> None:
    """Insert an issue_events row with a per-issue monotonic seq. Caller holds a txn."""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM issue_events WHERE issue_id = %s",
        (issue_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO issue_events (issue_id, seq, event_type, from_state, to_state, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (issue_id, seq, event_type, from_state, to_state, Jsonb(payload)),
    )
