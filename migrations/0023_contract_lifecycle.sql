-- =============================================================================
-- 0023_contract_lifecycle — lifecycle states, replacement metadata, and an
-- append-only lifecycle audit + idempotency layer for admin-driven contract
-- reconciliation. All writes go through repository.py (invariant #1). Additive:
-- no data migration, no CHECK constraint changes, _SATISFIED unchanged in code.
-- =============================================================================

-- Replacement pointer for supersede (and optional for retire).
ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS superseded_by_contract_id BIGINT
        REFERENCES contracts(id);

-- Idempotency + batch identity. Exactly one row per apply operation; the stored
-- response is replayed verbatim on a duplicate (project, operation_id).
CREATE TABLE IF NOT EXISTS contract_lifecycle_ops (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project       TEXT        NOT NULL DEFAULT '',
    operation_id  TEXT        NOT NULL,
    actor         TEXT        NOT NULL,
    actor_role    TEXT        NOT NULL,
    reason        TEXT        NOT NULL DEFAULT '',
    source        TEXT        NOT NULL DEFAULT '',   -- client/session metadata
    result        TEXT        NOT NULL,              -- applied | rejected | conflict
    requested     JSONB       NOT NULL,              -- the normalized change batch
    response      JSONB       NOT NULL,              -- the full apply result (for replay)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project, operation_id)
);

-- Append-only per-contract lifecycle events (one batch = N rows sharing op_id).
CREATE TABLE IF NOT EXISTS contract_lifecycle_events (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    op_id         BIGINT      NOT NULL REFERENCES contract_lifecycle_ops(id),
    contract_id   BIGINT      NOT NULL REFERENCES contracts(id),
    method        TEXT        NOT NULL,
    path          TEXT        NOT NULL,
    action        TEXT        NOT NULL,              -- agree|reject|deprecate|supersede|retire
    from_status   TEXT,
    to_status     TEXT        NOT NULL,
    superseded_by_contract_id BIGINT,
    reason        TEXT        NOT NULL DEFAULT '',
    actor         TEXT        NOT NULL,
    source_ref    TEXT,
    content_hash  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_contract
    ON contract_lifecycle_events(contract_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_op
    ON contract_lifecycle_events(op_id);
