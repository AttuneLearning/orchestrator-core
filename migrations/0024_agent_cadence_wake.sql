-- =============================================================================
-- 0024_agent_cadence_wake — durable-worker side-car cadence + wake relay
-- (plan §7, Phase 4). Additive only (safe on a live DB; no data touched).
--
-- active_window_seconds / dormant_interval_seconds: per-agent overrides of the
-- side-car CLI's --active-window/--dormant-interval defaults, editable from the
-- Agents page like poll_interval_seconds (migration 0009). agents.status is
-- plain unconstrained TEXT (see 0001_init.sql: 'idle | busy | offline' is just a
-- comment, no CHECK constraint) so adding the 'dormant' status value needs no
-- schema change here.
--
-- wake_signal: a per-project monotonically-increasing timestamp. The
-- orchestrator (or a human, via the dashboard "Wake all" button) bumps it after
-- promoting new work; every side-car polling GET /agents/{id}/pause?project=P
-- observes wake_at and fires an immediate tick (deduped on increase; see
-- Sidecar.check_wake) instead of waiting out its dormant cadence. One row per
-- project, upserted in place -- there is no history, only "the latest wake".
--
-- DEPLOY ORDER — MIGRATE BEFORE CODE (read this before restarting anything):
-- orchestrator/repository.py's _AGENT_COLS (the column list every agent query
-- selects) now includes active_window_seconds and dormant_interval_seconds
-- UNCONDITIONALLY — every get_agent/list_agents/register_agent/find_idle_agent
-- call, with no fallback path. Running the NEW code against an UN-migrated DB
-- (this migration not yet applied) means every one of those SELECTs raises
-- Postgres UndefinedColumn — i.e. a full outage of the dashboard, the engine's
-- agent-liveness/reclaim sweep, and the MCP heartbeat/list_my_work/my_queue
-- tools, on the very first agent query after (re)start. There is no
-- graceful degradation here; this is not optional ordering, it is a hard
-- prerequisite.
--
-- The correct order, every time, for every instance:
--   1. python -m orchestrator.cli --instance <name> migrate   (applies this
--      file; tracked in schema_migrations; safe to run repeatedly)
--   2. THEN restart that instance's dashboard/engine daemons (and any MCP
--      server processes pointed at that DB).
-- An already-running OLD-code process is unaffected by running the migration
-- early — it never selects the new columns until it restarts — so migrating
-- ahead of a restart is always safe; restarting NEW code ahead of migrating
-- is never safe. (The pytest `pool` fixture in tests/conftest.py auto-
-- migrates the test DB, so the test suite always exercises this migration
-- already applied and never observes the un-migrated ordering hazard.)
-- =============================================================================

ALTER TABLE agents ADD COLUMN IF NOT EXISTS active_window_seconds integer NOT NULL DEFAULT 1800;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS dormant_interval_seconds integer NOT NULL DEFAULT 3600;

CREATE TABLE IF NOT EXISTS wake_signal (
    project text PRIMARY KEY,
    wake_at timestamptz NOT NULL DEFAULT now()
);
