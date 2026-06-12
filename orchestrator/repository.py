"""Repository: the single place where SQL lives.

Both the engine and the MCP tool layer mutate state exclusively through these
functions, so every state change is recorded in the append-only issue_events
log that off-rails / oscillation detection depends on.

State transitions are written via update_state(), which inserts a matching
issue_events row in the same transaction as the issues update.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .models import Agent, Goal, Issue, IssueEvent, MemoryNote


# --------------------------------------------------------------------------- #
# Goals
# --------------------------------------------------------------------------- #

def create_goal(pool: ConnectionPool, title: str, description: str = "") -> Goal:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO goals (title, description, state)
            VALUES (%s, %s, 'backlog')
            RETURNING id, title, description, state, created_at, updated_at
            """,
            (title, description),
        ).fetchone()
    return Goal(*row)


def list_open_goals(pool: ConnectionPool) -> list[Goal]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            """
            SELECT id, title, description, state, created_at, updated_at
            FROM goals
            WHERE state NOT IN ('done', 'paused')
            ORDER BY id
            """
        ).fetchall()


def set_goal_state(pool: ConnectionPool, goal_id: int, state: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE goals SET state = %s, updated_at = now() WHERE id = %s",
            (state, goal_id),
        )


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
) -> Issue:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            """
            INSERT INTO issues
                (goal_id, parent_id, depth, team, title, description, pipeline,
                 state, triggered_by_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'backlog', %s)
            RETURNING id, goal_id, title, description, parent_id, depth, team,
                      pipeline, state, gate_type, retry_count, step_count,
                      assigned_agent, triggered_by_message, created_at, updated_at
            """,
            (goal_id, parent_id, depth, team, title, description, pipeline,
             triggered_by_message),
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
) -> list[Issue]:
    clauses, params = [], []
    if goal_id is not None:
        clauses.append("goal_id = %s")
        params.append(goal_id)
    if states:
        clauses.append("state = ANY(%s)")
        params.append(states)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with pool.connection() as conn:
        rows = conn.execute(_ISSUE_SELECT + where + " ORDER BY id", params).fetchall()
    return [_issue_from_row(r) for r in rows]


def count_issues_for_goal(pool: ConnectionPool, goal_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM issues WHERE goal_id = %s", (goal_id,)
        ).fetchone()[0]


def claim_issue(pool: ConnectionPool, issue_id: int, agent_id: int) -> None:
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE issues SET assigned_agent = %s, updated_at = now() WHERE id = %s",
            (agent_id, issue_id),
        )
        conn.execute(
            "UPDATE agents SET status = 'busy' WHERE id = %s", (agent_id,)
        )
        _append_event(conn, issue_id, "state_change", None, None,
                      {"claimed_by": agent_id})


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
            "triggered_by_message, created_at, updated_at",
            params,
        ).fetchone()
        _append_event(conn, issue_id, event_type, from_state, to_state, payload or {})
    return _issue_from_row(row)


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


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #

def register_agent(
    pool: ConnectionPool, team: str, function: str = "dev", runtime: str = "api"
) -> Agent:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO agents (team, function, runtime, status)
            VALUES (%s, %s, %s, 'idle')
            RETURNING id, team, function, runtime, status, created_at
            """,
            (team, function, runtime),
        ).fetchone()
    return Agent(*row)


def list_agents(pool: ConnectionPool, team: Optional[str] = None) -> list[Agent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        if team:
            return cur.execute(
                "SELECT id, team, function, runtime, status, created_at "
                "FROM agents WHERE team = %s ORDER BY id",
                (team,),
            ).fetchall()
        return cur.execute(
            "SELECT id, team, function, runtime, status, created_at "
            "FROM agents ORDER BY id"
        ).fetchall()


def set_agent_status(pool: ConnectionPool, agent_id: int, status: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE agents SET status = %s WHERE id = %s", (status, agent_id))


def find_idle_agent(
    pool: ConnectionPool, team: str, function: Optional[str] = None
) -> Optional[Agent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        if function:
            return cur.execute(
                "SELECT id, team, function, runtime, status, created_at FROM agents "
                "WHERE team = %s AND function = %s AND status != 'offline' "
                "ORDER BY (status = 'idle') DESC, id LIMIT 1",
                (team, function),
            ).fetchone()
        return cur.execute(
            "SELECT id, team, function, runtime, status, created_at FROM agents "
            "WHERE team = %s AND status != 'offline' "
            "ORDER BY (status = 'idle') DESC, id LIMIT 1",
            (team,),
        ).fetchone()


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #

def memory_write(pool: ConnectionPool, body: str, scope: str = "global") -> MemoryNote:
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


def memory_search(pool: ConnectionPool, query: str, limit: int = 20) -> list[MemoryNote]:
    """LIKE-based search for this phase; pgvector is a deferred follow-up."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(MemoryNote))
        return cur.execute(
            "SELECT id, scope, body, created_at FROM memory_notes "
            "WHERE body ILIKE %s ORDER BY id DESC LIMIT %s",
            (f"%{query}%", limit),
        ).fetchall()


# --------------------------------------------------------------------------- #
# ADRs & messages (skill-tool backing)
# --------------------------------------------------------------------------- #

def create_adr(
    pool: ConnectionPool, domain: str, title: str, decision: str = "", context: str = ""
) -> dict[str, Any]:
    with pool.connection() as conn, conn.transaction():
        n = conn.execute(
            "SELECT count(*) FROM adrs WHERE domain = %s", (domain,)
        ).fetchone()[0]
        adr_key = f"ADR-{domain.upper()}-{n + 1:03d}"
        row = conn.execute(
            "INSERT INTO adrs (adr_key, domain, title, decision, context) "
            "VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, adr_key, domain, title, status, decision, context, created_at",
            (adr_key, domain, title, decision, context),
        ).fetchone()
    cols = ["id", "adr_key", "domain", "title", "status", "decision", "context", "created_at"]
    return dict(zip(cols, row))


def create_message(
    pool: ConnectionPool,
    from_team: str,
    to_team: str,
    subject: str,
    body: str = "",
    priority: str = "medium",
    issue_id: Optional[int] = None,
) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO messages (from_team, to_team, subject, body, priority, issue_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id, from_team, to_team, subject, body, priority, issue_id, created_at",
            (from_team, to_team, subject, body, priority, issue_id),
        ).fetchone()
    cols = ["id", "from_team", "to_team", "subject", "body", "priority", "issue_id", "created_at"]
    return dict(zip(cols, row))


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

_ISSUE_SELECT = (
    "SELECT id, goal_id, title, description, parent_id, depth, team, pipeline, "
    "state, gate_type, retry_count, step_count, assigned_agent, "
    "triggered_by_message, created_at, updated_at FROM issues"
)


def _issue_from_row(row: tuple) -> Issue:
    return Issue(
        id=row[0], goal_id=row[1], title=row[2], description=row[3],
        parent_id=row[4], depth=row[5], team=row[6], pipeline=row[7],
        state=row[8], gate_type=row[9], retry_count=row[10], step_count=row[11],
        assigned_agent=row[12], triggered_by_message=row[13],
        created_at=row[14], updated_at=row[15],
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
