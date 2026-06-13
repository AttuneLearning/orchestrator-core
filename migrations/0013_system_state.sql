-- =============================================================================
-- 0013_system_state — tiny key/value store for orchestrator bookkeeping.
--
-- First use: the periodic `git-review` job records `last_reviewed_sha` here so
-- it only alerts once per upstream commit (no alert spam). General-purpose kv;
-- app-managed updated_at. Additive only.
-- =============================================================================

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
