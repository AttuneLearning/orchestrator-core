"""Database connection pool and migration runner.

Uses psycopg 3 with a connection pool. migrate() applies migrations/*.sql in
filename order, tracking applied files in a schema_migrations table so it is
idempotent and reproducible without Docker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from psycopg import Connection
from psycopg_pool import ConnectionPool

from .config import MIGRATIONS_DIR, Settings

_pool: Optional[ConnectionPool] = None


def get_pool(settings: Settings) -> ConnectionPool:
    """Return a process-wide connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=settings.database_url, min_size=1, max_size=8, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _ensure_migrations_table(conn: Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def migrate(settings: Settings, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending .sql migrations in order. Returns the filenames applied."""
    pool = get_pool(settings)
    applied: list[str] = []
    files = sorted(p for p in migrations_dir.glob("*.sql"))
    with pool.connection() as conn:
        _ensure_migrations_table(conn)
        done = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
        for path in files:
            if path.name in done:
                continue
            sql = path.read_text()
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            applied.append(path.name)
    return applied
