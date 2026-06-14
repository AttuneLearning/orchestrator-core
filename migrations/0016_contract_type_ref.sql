-- Phase 3 (contract precision): a per-endpoint pointer to the contract's type
-- document in the shared packages/contracts package (the monorepo SSOT), e.g.
-- 'packages/contracts/types/curriculum.ts'. Metadata only — deliberately NOT part
-- of the content_hash (shape identity stays method|path|request_ref|response_dto),
-- so backfilling/refreshing type_ref never registers as contract drift.
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS type_ref text;
ALTER TABLE contract_proposals ADD COLUMN IF NOT EXISTS type_ref text;
