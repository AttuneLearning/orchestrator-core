-- =============================================================================
-- 0014_message_inbox — read/unread state + thread links for the my_queue inbox.
--
-- my_queue(agent_id) returns a worker's assigned issues PLUS unread inbound
-- messages for its team. read_at marks a message consumed (NULL = unread) so it
-- drops out of the queue once handled. reply_to links a response to the request
-- it answers, so from any message the full thread (request <-> response) plus its
-- issue_id is discoverable. Additive only.
-- =============================================================================

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS read_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reply_to BIGINT REFERENCES messages(id) ON DELETE SET NULL;
