-- =============================================================================
-- 0015_contract_proposals — staging layer for contract master-data management.
--
-- Imports / backend deliveries no longer overwrite the live `contracts` store;
-- they STAGE changes here as proposals (add | modify | remove) diffed against the
-- accepted contracts. A human reviews current-vs-proposed on /contracts and
-- accepts — only acceptance writes the `contracts` row (status agreed/live) and
-- satisfies the contract_check gate. At most one PENDING proposal per (method,
-- path); re-import replaces pending ones. Additive only.
-- =============================================================================

CREATE TABLE IF NOT EXISTS contract_proposals (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method        TEXT        NOT NULL,
    path          TEXT        NOT NULL,
    change_type   TEXT        NOT NULL,                    -- add | modify | remove
    request_ref   TEXT        NOT NULL DEFAULT '',
    response_dto  TEXT        NOT NULL DEFAULT '',
    auth          TEXT        NOT NULL DEFAULT 'none',
    owner_team    TEXT        NOT NULL DEFAULT 'backend',
    version       TEXT        NOT NULL DEFAULT '1.0',
    target_status TEXT        NOT NULL DEFAULT 'live',      -- status to set on accept
    content_hash  TEXT,
    source_ref    TEXT,
    status        TEXT        NOT NULL DEFAULT 'pending',   -- pending | accepted | rejected
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);
-- one pending proposal per endpoint (partial unique index)
CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_proposals_pending
    ON contract_proposals(method, path) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_contract_proposals_status ON contract_proposals(status);
