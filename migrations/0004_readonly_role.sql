-- =============================================================================
-- 0004_readonly_role — create orchestrator_ro read-only role for analytics
-- tools (Metabase, BI queries).  Fully idempotent: safe to re-run.
-- =============================================================================

-- Create the role only when it does not already exist.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator_ro'
    ) THEN
        CREATE ROLE orchestrator_ro
            LOGIN
            PASSWORD 'orchestrator_ro';
    END IF;
END
$$;

-- Allow the role to connect to the orchestrator database.
GRANT CONNECT ON DATABASE orchestrator TO orchestrator_ro;

-- Allow the role to see objects in the public schema.
GRANT USAGE ON SCHEMA public TO orchestrator_ro;

-- Grant SELECT on all existing tables in the public schema.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO orchestrator_ro;

-- Ensure SELECT is automatically granted on tables created in the future
-- (e.g. by subsequent migrations or by Directus' directus_* tables).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO orchestrator_ro;
