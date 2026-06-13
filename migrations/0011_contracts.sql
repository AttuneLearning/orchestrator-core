-- =============================================================================
-- 0011_contracts — API contract store + per-issue contract dependencies.
--
-- The orchestrator gates frontend work on agreed API contracts (contract-first):
-- one row per endpoint, keyed (method, path). The DB holds the coordination
-- layer the engine gates on (status + pointers + hash); the authoritative
-- schema content stays in the owning repo (OpenAPI / shared types). Lifecycle:
-- proposed (shape requested, inert) → agreed (FE may build against it) →
-- live (endpoint actually implemented) → deprecated. contract_satisfied =
-- status in ('agreed','live').
--
-- issue_contract_deps records what a blocked frontend issue is waiting on. We
-- block on contract *status* (not on the backend issue), so frontend and backend
-- proceed in parallel: the engine's _unblock releases the issue as soon as every
-- needed (method,path) reaches an agreed/live contract. Additive only.
-- =============================================================================

CREATE TABLE IF NOT EXISTS contracts (
    id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method       TEXT        NOT NULL,                       -- GET | POST | PUT | PATCH | DELETE
    path         TEXT        NOT NULL,                       -- e.g. /system/status
    request_ref  TEXT        NOT NULL DEFAULT '',            -- pointer: validator/schema export name
    response_dto TEXT        NOT NULL DEFAULT '',            -- pointer: response DTO type name
    auth         TEXT        NOT NULL DEFAULT 'none',
    owner_team   TEXT        NOT NULL DEFAULT 'backend',
    status       TEXT        NOT NULL DEFAULT 'proposed',    -- proposed | agreed | live | deprecated
    version      TEXT        NOT NULL DEFAULT '1.0',
    content_hash TEXT,                                       -- sha256 of {method,path,request_ref,response_dto}
    source_ref   TEXT,                                       -- catalog section | route file:line | commit
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (method, path)
);
CREATE INDEX IF NOT EXISTS idx_contracts_owner_team ON contracts(owner_team);
CREATE INDEX IF NOT EXISTS idx_contracts_status     ON contracts(status);

CREATE TABLE IF NOT EXISTS issue_contract_deps (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_id   BIGINT      NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    method     TEXT        NOT NULL,
    path       TEXT        NOT NULL,
    satisfied  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_issue_contract_deps_issue ON issue_contract_deps(issue_id);
