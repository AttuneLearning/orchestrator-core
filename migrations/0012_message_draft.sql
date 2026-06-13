-- =============================================================================
-- 0012_message_draft — cache the orchestration-monitor's suggested reply.
--
-- The /orch/monitor dashboard drafts a suggested response (via the configured
-- reasoner) to each pending message addressed to the orchestration/monitor team.
-- We cache that draft on the message row so the page doesn't re-call the model on
-- every load; a human reviews it (or overrides) before it's sent. Additive only.
-- =============================================================================

ALTER TABLE messages ADD COLUMN IF NOT EXISTS draft_response TEXT;
