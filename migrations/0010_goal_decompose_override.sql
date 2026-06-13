-- =============================================================================
-- 0010_goal_decompose_override — per-goal decomposition override (spec: fix 1).
-- Additive only (safe on a live DB; no data touched). decompose is NULL by
-- default (use the simple-goal heuristic); an operator may force the fast-path
-- ('single' → exactly one implementation issue) or full decomposition ('full',
-- still bounded by the engine's depth/size caps). goals.decompose is
-- unconstrained TEXT, so the two sentinel values need no constraint.
-- =============================================================================

ALTER TABLE goals ADD COLUMN IF NOT EXISTS decompose TEXT;
