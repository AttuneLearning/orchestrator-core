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

from .models import Agent, Goal, Issue, IssueEvent, IssueState, MemoryNote
from .state_machine import validate_transition


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


def list_all_goals(pool: ConnectionPool) -> list[Goal]:
    """Every goal regardless of state — for the dashboard, which must show paused
    and done goals that list_open_goals deliberately hides from the engine."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            "SELECT id, title, description, state, created_at, updated_at "
            "FROM goals ORDER BY id"
        ).fetchall()


def set_goal_state(pool: ConnectionPool, goal_id: int, state: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE goals SET state = %s, updated_at = now() WHERE id = %s",
            (state, goal_id),
        )


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
                      created_at, updated_at
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
            "triggered_by_message, origin_message_id, created_at, updated_at",
            params,
        ).fetchone()
        _append_event(conn, issue_id, event_type, from_state, to_state, payload or {})
    return _issue_from_row(row)


def apply_directive(
    pool: ConnectionPool,
    issue_id: int,
    directive: str = "resume",
    note: str = "",
    actor: str = "human",
) -> Issue:
    """Human directive: un-quarantine an off_rails issue (off_rails → in_progress).

    The only path out of the off_rails latch. Resets retry/step counters and
    keeps the gate, so work resumes where it was quarantined. Recorded as a
    'directive' event — the focus sweep only considers events after the latest
    directive, so the issue gets a genuinely fresh start.
    """
    issue = get_issue(pool, issue_id)
    if issue is None:
        raise ValueError(f"no issue {issue_id}")
    to_state = IssueState.IN_PROGRESS.value
    if issue.state != IssueState.OFF_RAILS.value or not validate_transition(
        issue.state, to_state, directive=True
    ):
        raise ValueError(
            f"directive '{directive}' not applicable: issue {issue_id} is "
            f"'{issue.state}', expected 'off_rails'"
        )
    return update_state(
        pool, issue_id, to_state, gate_type=issue.gate_type,
        event_type="directive",
        payload={"directive": directive, "note": note, "actor": actor},
        retry_count=0, step_count=0,
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


_MESSAGE_COLS = ["id", "from_team", "to_team", "subject", "body", "priority",
                 "issue_id", "kind", "status", "created_at"]
_MESSAGE_SELECT = ("SELECT id, from_team, to_team, subject, body, priority, "
                   "issue_id, kind, status, created_at FROM messages")


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
) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO messages (from_team, to_team, subject, body, priority, "
            "issue_id, kind, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, from_team, to_team, subject, body, priority, issue_id, "
            "kind, status, created_at",
            (from_team, to_team, subject, body, priority, issue_id, kind, status),
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


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

_ISSUE_SELECT = (
    "SELECT id, goal_id, title, description, parent_id, depth, team, pipeline, "
    "state, gate_type, retry_count, step_count, assigned_agent, "
    "triggered_by_message, origin_message_id, created_at, updated_at FROM issues"
)


def _issue_from_row(row: tuple) -> Issue:
    return Issue(
        id=row[0], goal_id=row[1], title=row[2], description=row[3],
        parent_id=row[4], depth=row[5], team=row[6], pipeline=row[7],
        state=row[8], gate_type=row[9], retry_count=row[10], step_count=row[11],
        assigned_agent=row[12], triggered_by_message=row[13],
        origin_message_id=row[14], created_at=row[15], updated_at=row[16],
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
