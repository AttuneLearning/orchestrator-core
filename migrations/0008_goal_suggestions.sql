-- =============================================================================
-- 0008_goal_suggestions — gated proposal channel for external looping agents.
-- External agents (via MCP propose_goal) create goals in the 'suggested' state,
-- which the engine ignores (_decompose only picks 'backlog'); a human promotes
-- them to 'backlog' or rejects them. These columns record who suggested a goal
-- and why. goals.state is unconstrained TEXT, so 'suggested'/'rejected' need no
-- constraint change.
-- =============================================================================

ALTER TABLE goals
    ADD COLUMN IF NOT EXISTS suggested_by TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source       TEXT NOT NULL DEFAULT '';
