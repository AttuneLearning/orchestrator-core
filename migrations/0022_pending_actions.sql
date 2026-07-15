-- WP-11: Pending actions table for escalation persistence.
-- Tracks actions awaiting approval before execution.
CREATE TABLE pending_actions (
    id BIGSERIAL PRIMARY KEY,
    issue_id BIGINT REFERENCES issues(id) ON DELETE CASCADE,
    worktree TEXT NOT NULL,
    step TEXT NOT NULL,
    action TEXT NOT NULL,                       -- exact resolved run string or builtin name
    action_kind TEXT NOT NULL DEFAULT 'run',    -- run | builtin
    requested_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',     -- pending|approved|denied|expired|executed
    resolved_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours'
);
CREATE INDEX pending_actions_status_idx ON pending_actions (status, expires_at);
