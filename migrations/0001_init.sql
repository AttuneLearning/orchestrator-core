-- =============================================================================
-- 0001_init — canonical orchestrator schema.
-- Plain SQL, applied in order by orchestrator/db.py:migrate(). Reproducible
-- without Docker against any Postgres 16/17.
-- =============================================================================

-- Goals: top-level objectives ingested into the orchestrator.
CREATE TABLE IF NOT EXISTS goals (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT        NOT NULL,
    description TEXT        NOT NULL DEFAULT '',
    state       TEXT        NOT NULL DEFAULT 'backlog',   -- backlog|planning|active|paused|done
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- agents: registry of worker/reasoning agents.
CREATE TABLE IF NOT EXISTS agents (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team       TEXT        NOT NULL,
    function   TEXT        NOT NULL DEFAULT 'dev',          -- dev | qa
    runtime    TEXT        NOT NULL DEFAULT 'api',          -- api | cli (cli deferred)
    status     TEXT        NOT NULL DEFAULT 'idle',         -- idle | busy | offline
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Issues: discrete work items. May nest via parent_id (sub-issue decomposition).
CREATE TABLE IF NOT EXISTS issues (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    goal_id        BIGINT      NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    parent_id      BIGINT      REFERENCES issues(id) ON DELETE CASCADE,
    depth          INT         NOT NULL DEFAULT 0,
    team           TEXT        NOT NULL DEFAULT 'backend',
    title          TEXT        NOT NULL,
    description    TEXT        NOT NULL DEFAULT '',
    pipeline       TEXT        NOT NULL DEFAULT 'pipeline-1',
    state          TEXT        NOT NULL DEFAULT 'backlog',  -- see state_machine.IssueState
    gate_type      TEXT,                                    -- current gate when in_review/in_progress
    retry_count    INT         NOT NULL DEFAULT 0,
    step_count     INT         NOT NULL DEFAULT 0,
    assigned_agent BIGINT      REFERENCES agents(id) ON DELETE SET NULL,
    triggered_by_message BOOLEAN NOT NULL DEFAULT FALSE,    -- gates the conditional comms_response gate
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_issues_goal  ON issues(goal_id);
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);

-- issue_events: append-only processing log. Source of truth for oscillation
-- detection, off-rails signals, audit, and timeline reconstruction.
CREATE TABLE IF NOT EXISTS issue_events (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_id   BIGINT      NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    seq        INT         NOT NULL,                        -- per-issue monotonic sequence
    event_type TEXT        NOT NULL,                        -- see models.EventType
    from_state TEXT,
    to_state   TEXT,
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issue_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_issue ON issue_events(issue_id);

-- memory_notes: flat scoped memory. embedding reserved for pgvector (phase 2).
CREATE TABLE IF NOT EXISTS memory_notes (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope      TEXT        NOT NULL DEFAULT 'global',        -- global | pod:* | agent:*
    body       TEXT        NOT NULL,
    embedding  BYTEA,                                        -- nullable; pgvector drop-in later
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_notes(scope);

-- messages: cross-team message mirror (file-based dev_communication counterpart).
CREATE TABLE IF NOT EXISTS messages (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_team  TEXT        NOT NULL,
    to_team    TEXT        NOT NULL,
    subject    TEXT        NOT NULL,
    body       TEXT        NOT NULL DEFAULT '',
    priority   TEXT        NOT NULL DEFAULT 'medium',
    issue_id   BIGINT      REFERENCES issues(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- adrs: architecture decision records.
CREATE TABLE IF NOT EXISTS adrs (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    adr_key    TEXT        NOT NULL UNIQUE,                  -- ADR-{DOMAIN}-{NNN}
    domain     TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    status     TEXT        NOT NULL DEFAULT 'accepted',
    decision   TEXT        NOT NULL DEFAULT '',
    context    TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
