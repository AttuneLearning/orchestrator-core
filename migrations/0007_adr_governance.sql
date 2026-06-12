-- =============================================================================
-- 0007_adr_governance — ADR rules govern agent work (ADR governance slice).
--
-- adrs become selectable rules: applies_to scopes a rule to work-types, teams,
-- and repos (empty dimension = matches all; empty repos = project-wide).
-- decision holds the compact directive agents receive; context holds the
-- rationale (humans only). Backlink edges (related/supersedes/patterns) power
-- the dashboard graph. Lifecycle: proposed (inert) → accepted (live) →
-- deprecated | superseded.
-- issues.work_type is tagged at planning time and drives rule selection.
-- =============================================================================

ALTER TABLE adrs
    ADD COLUMN IF NOT EXISTS applies_to  JSONB  NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS related     TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS supersedes  TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS patterns    TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS proposed_by TEXT   NOT NULL DEFAULT 'human';

ALTER TABLE issues
    ADD COLUMN IF NOT EXISTS work_type TEXT;
