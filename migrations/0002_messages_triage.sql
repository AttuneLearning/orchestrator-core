-- =============================================================================
-- 0002_messages_triage — comms ingestion (slice D).
--
-- messages gain a kind + triage status so the engine can ingest inbound
-- requests into local issues (PROCESS_GUIDE: "the decision to create an issue
-- is ALWAYS the receiving team's") without re-ingesting its own responses.
-- issues gain origin_message_id so the comms_response gate can find the inbox
-- to answer and archive.
-- =============================================================================

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS kind   TEXT NOT NULL DEFAULT 'request',   -- request | response
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';   -- pending | triaged | rejected | archived | sent

CREATE INDEX IF NOT EXISTS idx_messages_pending
    ON messages(to_team) WHERE status = 'pending' AND kind = 'request';

ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS origin_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL;
