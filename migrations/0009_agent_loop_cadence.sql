-- =============================================================================
-- 0009_agent_loop_cadence — per-agent pull-loop policy (token-spend control).
-- Additive only (safe on a live DB; no data touched). loop_enabled gates whether
-- a pull worker keeps polling after its queue drains; poll_interval_seconds is the
-- idle cadence when enabled. Disabled = the worker stops after draining (no slow
-- poll). The orchestrator publishes these via heartbeat; the worker self-paces
-- (pull model — the engine cannot push a wake). Token-safe default: loop off.
-- =============================================================================

ALTER TABLE agents ADD COLUMN IF NOT EXISTS loop_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS poll_interval_seconds integer NOT NULL DEFAULT 300;
