"""Repository: the single place where SQL lives.

Both the engine and the MCP tool layer mutate state exclusively through these
functions, so every state change is recorded in the append-only issue_events
log that off-rails / oscillation detection depends on.

State transitions are written via update_state(), which inserts a matching
issue_events row in the same transaction as the issues update.
"""

from __future__ import annotations

import hashlib
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


def unassign_issue(pool: ConnectionPool, issue_id: int) -> None:
    """Clear an issue's assigned agent (does not touch agent status). Used when a
    pull gate has no eligible external worker, so the gate isn't falsely 'owned'
    by a carried-over agent from the previous gate."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE issues SET assigned_agent = NULL, updated_at = now() WHERE id = %s",
            (issue_id,),
        )


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
               "loop_enabled, poll_interval_seconds")


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


def agent_next_poll_seconds(agent: Agent) -> int:
    """Idle cadence the worker should obey: the poll interval when looping is
    enabled, else 0 — meaning 'stop after the queue drains' (disabled = stop)."""
    return agent.poll_interval_seconds if agent.loop_enabled else 0


def set_agent_loop(pool: ConnectionPool, agent_id: int, *,
                   loop_enabled: Optional[bool] = None,
                   poll_interval_seconds: Optional[int] = None) -> Optional[Agent]:
    """Set a pull worker's loop policy. Only provided fields change. Interval is
    bounded to 60..7200s (reject out-of-range)."""
    sets: list[str] = []
    params: list[Any] = []
    if loop_enabled is not None:
        sets.append("loop_enabled = %s"); params.append(loop_enabled)
    if poll_interval_seconds is not None:
        if not (60 <= poll_interval_seconds <= 7200):
            raise ValueError("poll_interval_seconds must be between 60 and 7200")
        sets.append("poll_interval_seconds = %s"); params.append(poll_interval_seconds)
    if sets:
        params.append(agent_id)
        with pool.connection() as conn:
            conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = %s", params)
    return get_agent(pool, agent_id)


def find_idle_agent(
    pool: ConnectionPool, team: str, function: Optional[str] = None,
    runtime: Optional[str] = None,
) -> Optional[Agent]:
    """Pick an available agent for a team. `function` (dev/qa/lead) and `runtime`
    (api/cli/external) narrow the search; idle agents rank ahead of busy ones.
    Pull gates pass runtime='external' to require a live worker."""
    clauses = ["team = %s", "status != 'offline'"]
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
            "ORDER BY (status = 'idle') DESC, id LIMIT 1",
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
    with pool.connection() as conn, conn.transaction():
        # Next number = max existing suffix in this domain + 1, NOT count(*).
        # Count-based numbering collides after a delete/supersede (the trailing
        # number can already be in use); adr_key is UNIQUE so that would error.
        n = conn.execute(
            "SELECT COALESCE(MAX(substring(adr_key from '[0-9]+$')::int), 0) "
            "FROM adrs WHERE domain = %s", (domain,)
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
    """Mark a message consumed so it drops out of my_queue (read_at = now())."""
    with pool.connection() as conn:
        conn.execute("UPDATE messages SET read_at = now() WHERE id = %s", (message_id,))


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
                  "type_ref", "created_at", "updated_at"]
_CONTRACT_SELECT = "SELECT " + ", ".join(_CONTRACT_COLS) + " FROM contracts"
_SATISFIED = ("agreed", "live")


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
) -> dict[str, Any]:
    """Record a 'proposed' contract a consumer needs. If one already exists it is
    left untouched (never downgrades an agreed/live contract) and returned as-is."""
    method = method.upper()
    content_hash = _contract_hash(method, path, request_ref, response_dto)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO contracts (method, path, request_ref, response_dto, auth, "
            "owner_team, status, content_hash, source_ref, type_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s, %s) "
            "ON CONFLICT (method, path) DO NOTHING",
            (method, path, request_ref, response_dto, auth, owner_team,
             content_hash, source_ref, type_ref),
        )
    return get_contract(pool, method, path)  # type: ignore[return-value]


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
    """Accept the contract row currently visible on the /contracts page."""
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
