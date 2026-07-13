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

-- Allow the role to connect to THIS database, whatever it is named. The DB name
-- is not knowable statically (each instance in config/instances.yaml uses its own
-- database, e.g. `tendcharting`, `myproject`), so grant against current_database()
-- via dynamic SQL rather than a hardcoded name — a hardcoded name errors on any
-- database not literally called `orchestrator`, which would break `migrate` for
-- every adopter who names their DB after their own project.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO orchestrator_ro', current_database());
END
$$;

-- Allow the role to see objects in the public schema.
GRANT USAGE ON SCHEMA public TO orchestrator_ro;

-- Grant SELECT on all existing tables in the public schema.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO orchestrator_ro;

-- Ensure SELECT is automatically granted on tables created in the future
-- (e.g. by subsequent migrations or by Directus' directus_* tables).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO orchestrator_ro;
