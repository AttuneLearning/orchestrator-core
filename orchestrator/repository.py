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

def create_goal(pool: ConnectionPool, title: str, description: str = "",
                pipeline: str = "pipeline-1") -> Goal:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO goals (title, description, state, pipeline)
            VALUES (%s, %s, 'backlog', %s)
            RETURNING id, title, description, state, pipeline, created_at, updated_at
            """,
            (title, description, pipeline),
        ).fetchone()
    return Goal(*row)


def list_open_goals(pool: ConnectionPool) -> list[Goal]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Goal))
        return cur.execute(
            """
            SELECT id, title, description, state, pipeline, created_at, updated_at
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
            "SELECT id, title, description, state, pipeline, created_at, updated_at "
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
            "triggered_by_message, origin_message_id, work_type, created_at, updated_at",
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

_AGENT_COLS = "id, team, function, runtime, status, last_seen, created_at"


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


def touch_agent(pool: ConnectionPool, agent_id: int) -> None:
    """Heartbeat: record that the agent did work just now."""
    with pool.connection() as conn:
        conn.execute("UPDATE agents SET last_seen = now() WHERE id = %s", (agent_id,))


def find_idle_agent(
    pool: ConnectionPool, team: str, function: Optional[str] = None
) -> Optional[Agent]:
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Agent))
        if function:
            return cur.execute(
                f"SELECT {_AGENT_COLS} FROM agents "
                "WHERE team = %s AND function = %s AND status != 'offline' "
                "ORDER BY (status = 'idle') DESC, id LIMIT 1",
                (team, function),
            ).fetchone()
        return cur.execute(
            f"SELECT {_AGENT_COLS} FROM agents "
            "WHERE team = %s AND status != 'offline' "
            "ORDER BY (status = 'idle') DESC, id LIMIT 1",
            (team,),
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


def memory_search(
    pool: ConnectionPool,
    query: str,
    limit: int = 20,
    query_embedding: Optional[list[float]] = None,
) -> list[MemoryNote]:
    """Search memory notes.

    If query_embedding is provided AND pgvector is available, uses cosine
    distance (embedding_v <=> %s::vector) with a WHERE embedding_v IS NOT NULL
    filter.  Falls back to ILIKE on the query string for rows with no vector,
    appending any non-duplicate ILIKE hits up to the limit.

    When pgvector is unavailable or query_embedding is None, uses ILIKE only
    (original behaviour — all existing call sites continue to work).
    """
    if query_embedding is not None and _pgvector_available(pool):
        vec_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"
        with pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(MemoryNote))
            # Primary: vector-similarity results (rows that have an embedding).
            vec_rows = cur.execute(
                "SELECT id, scope, body, created_at FROM memory_notes "
                "WHERE embedding_v IS NOT NULL "
                "ORDER BY embedding_v <=> %s::vector "
                "LIMIT %s",
                (vec_literal, limit),
            ).fetchall()
            seen_ids = {n.id for n in vec_rows}
            results = list(vec_rows)
            # Supplement with ILIKE hits if we still have room and the query
            # text is not empty (degenerate queries fall back entirely to ILIKE).
            if len(results) < limit and query:
                remaining = limit - len(results)
                ilike_rows = cur.execute(
                    "SELECT id, scope, body, created_at FROM memory_notes "
                    "WHERE body ILIKE %s ORDER BY id DESC LIMIT %s",
                    (f"%{query}%", remaining + len(seen_ids)),
                ).fetchall()
                for n in ilike_rows:
                    if n.id not in seen_ids and len(results) < limit:
                        results.append(n)
        return results
    # Fallback: original ILIKE behaviour (unchanged).
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
    with pool.connection() as conn, conn.transaction():
        n = conn.execute(
            "SELECT count(*) FROM adrs WHERE domain = %s", (domain,)
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
        clauses.append("domain = %s")
        params.append(domain)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with pool.connection() as conn:
        rows = conn.execute(_ADR_SELECT + where + " ORDER BY adr_key", params).fetchall()
    return [dict(zip(_ADR_COLS, r)) for r in rows]


def get_adr(pool: ConnectionPool, adr_key: str) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(_ADR_SELECT + " WHERE adr_key = %s", (adr_key,)).fetchone()
    return dict(zip(_ADR_COLS, row)) if row else None


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
