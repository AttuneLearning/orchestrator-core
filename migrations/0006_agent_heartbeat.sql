-- =============================================================================
-- 0006_agent_heartbeat — agents report liveness (slice I).
-- Workers touch last_seen whenever they do work; the dashboard flags agents
-- whose heartbeat is stale.
-- =============================================================================

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
