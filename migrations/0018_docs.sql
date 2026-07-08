-- =============================================================================
-- 0018_docs — shared cross-agent development docs, stored in the DB (not the
-- filesystem) so there is ONE canonical store reachable identically from every
-- worktree via MCP (doc_* tools) and the dashboard "Docs" tab. Mirrors how adrs,
-- contracts, and memory_notes already live in the DB. Supersedes the filesystem
-- docs/ tree (per ADR-ORCH-008): no per-worktree copies, no location ambiguity.
--
--   path    — logical location/slug, unique (e.g. 'architecture/knowledge-packets').
--   title   — human title shown in lists/headers.
--   body    — the document content.
--   format  — 'markdown' | 'html' | 'text' (how the dashboard renders it).
--   author  — who last wrote it (team/agent/human label).
-- updated_at bumps on every write so lists can sort newest-first.
-- =============================================================================
CREATE TABLE IF NOT EXISTS docs (
    id          BIGSERIAL PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    format      TEXT NOT NULL DEFAULT 'markdown',
    author      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS docs_updated_idx ON docs (updated_at DESC);
