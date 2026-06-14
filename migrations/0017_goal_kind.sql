-- Maintenance lane (v1: standing backlog). A goal kind that backfills idle
-- capacity: kind='maintenance' goals are perpetual standing backlogs — the engine
-- never auto-completes or pauses them in _reconcile, and their issues are assigned
-- to a team's idle worker only when that team has no 'standard' work pending
-- (see engine.focus.select_assignable). Default keeps every existing goal standard.
ALTER TABLE goals ADD COLUMN kind text NOT NULL DEFAULT 'standard';
