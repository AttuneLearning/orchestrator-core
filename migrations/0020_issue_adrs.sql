-- issue_adrs: which accepted ADRs the reasoner related to an issue, computed
-- ONCE at decomposition/creation and cached. adr_for_issue unions these with the
-- deterministic selector matches + the full backlink closure, so a worker pulls
-- only the ADRs that govern its issue (not the whole catalog).
CREATE TABLE IF NOT EXISTS issue_adrs (
    issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    adr_key    TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'reasoner',  -- reasoner | selector | human
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_id, adr_key)
);
CREATE INDEX IF NOT EXISTS idx_issue_adrs_issue ON issue_adrs(issue_id);
