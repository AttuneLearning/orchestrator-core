-- =============================================================================
-- 0019_agent_pause — per-agent cooldown / auto-retry window.
-- Additive only (safe on a live DB). `paused_until` (nullable): when set to a
-- future timestamp the engine will not assign new work to the agent and a pull
-- worker sleeps until then, then resumes automatically. Used for token-limit
-- backoff (the worker sets now()+2h when the model server signals a limit) and
-- for manual pauses from the dashboard. NULL or a past time = active.
-- =============================================================================

ALTER TABLE agents ADD COLUMN IF NOT EXISTS paused_until timestamptz;
