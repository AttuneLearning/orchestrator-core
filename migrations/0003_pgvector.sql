-- =============================================================================
-- 0003_pgvector — optional semantic-search column for memory_notes (slice H).
--
-- Uses a DO block so the migration succeeds (as a no-op with RAISE NOTICE)
-- on any Postgres instance where pgvector is not installed.  When pgvector IS
-- present the extension is enabled and an embedding_v vector(256) column is
-- added to memory_notes; an IVFFlat index is also created so that cosine
-- searches are fast at scale.
--
-- The legacy BYTEA `embedding` column introduced in 0001_init is intentionally
-- left alone (backward compat).
-- =============================================================================

DO $$
BEGIN
    -- 1. Try to enable the extension.
    BEGIN
        CREATE EXTENSION IF NOT EXISTS vector;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pgvector not available (%), skipping semantic-memory setup', SQLERRM;
        RETURN;
    END;

    -- 2. Add the typed vector column (idempotent).
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memory_notes' AND column_name = 'embedding_v'
    ) THEN
        ALTER TABLE memory_notes ADD COLUMN embedding_v vector(256);
    END IF;

    -- 3. IVFFlat cosine index (skip if already present).
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'memory_notes' AND indexname = 'idx_memory_embedding_v'
    ) THEN
        CREATE INDEX idx_memory_embedding_v
            ON memory_notes USING ivfflat (embedding_v vector_cosine_ops)
            WITH (lists = 100);
    END IF;

    RAISE NOTICE 'pgvector semantic-memory setup complete.';
END;
$$;
