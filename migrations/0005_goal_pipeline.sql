-- =============================================================================
-- 0005_goal_pipeline — goals choose a pipeline (slice J).
-- Issues decomposed from a goal inherit its pipeline; ingested-message goals
-- keep the default. Existing rows backfill to pipeline-1.
-- =============================================================================

ALTER TABLE goals
    ADD COLUMN IF NOT EXISTS pipeline TEXT NOT NULL DEFAULT 'pipeline-1';
