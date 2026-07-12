-- GAP-2: track who proposed a contract so worker proposals can be rate-limited
-- (the junk-ADR loop failure mode, aimed at the contracts SSOT).
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS proposed_by TEXT NOT NULL DEFAULT 'human';
CREATE INDEX IF NOT EXISTS idx_contracts_proposer_time ON contracts(proposed_by, created_at);
