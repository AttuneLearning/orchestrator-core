"""Coordinator database backup helpers.

The preferred backup is pg_dump's custom format. Some dev boxes do not have the
Postgres client tools installed, so the fallback writes a gzip-compressed JSON
logical dump of every public table. The fallback is enough to preserve the
orchestrator state for development recovery; install pg_dump for production-grade
restore tooling.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import repository as repo
from .config import REPO_ROOT, Settings


def _backup_dir(settings: Settings) -> Path:
    path = Path(settings.database_backup_dir).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_label(database_url: str) -> str:
    parsed = urlparse(database_url)
    name = (parsed.path or "").rsplit("/", 1)[-1] or "orchestrator"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json_fallback(settings: Settings, reason: str) -> dict[str, Any]:
    out = _backup_dir(settings) / f"{_stamp()}-{_database_label(settings.database_url)}-{reason}.json.gz"
    payload: dict[str, Any] = {
        "format": "orchestrator-json-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "database": _database_label(settings.database_url),
        "tables": {},
    }
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        tables = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        ).fetchall()
        for table in tables:
            table_name = table["table_name"]
            rows = conn.execute(
                f'SELECT * FROM "{table_name}" ORDER BY 1'
            ).fetchall()
            payload["tables"][table_name] = rows

    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str, separators=(",", ":"))
    os.chmod(out, 0o600)
    return {
        "passed": True,
        "method": "json-fallback",
        "path": str(out),
        "bytes": out.stat().st_size,
        "reason": reason,
    }


def backup_database(settings: Settings, *, reason: str = "manual") -> dict[str, Any]:
    """Run a coordinator DB backup and return a structured result."""
    if not settings.database_backup_enabled:
        return {"passed": True, "skipped": "database backups disabled", "reason": reason}

    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason).strip("_") or "manual"
    pg_dump = shutil.which("pg_dump")
    if pg_dump:
        out = _backup_dir(settings) / f"{_stamp()}-{_database_label(settings.database_url)}-{safe_reason}.dump"
        result = subprocess.run(
            [pg_dump, "--format=custom", "--file", str(out), settings.database_url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            os.chmod(out, 0o600)
            return {
                "passed": True,
                "method": "pg_dump-custom",
                "path": str(out),
                "bytes": out.stat().st_size,
                "reason": reason,
            }
        if out.exists():
            out.unlink()
        return {
            "passed": False,
            "method": "pg_dump-custom",
            "reason": reason,
            "error": (result.stderr or result.stdout or "pg_dump failed").strip(),
        }

    return _write_json_fallback(settings, safe_reason)


def record_backup(
    pool: ConnectionPool,
    settings: Settings,
    *,
    reason: str,
    issue_id: int | None = None,
    goal_id: int | None = None,
) -> dict[str, Any]:
    """Run backup best-effort and record success/failure in orchestrator state."""
    try:
        result = backup_database(settings, reason=reason)
    except Exception as exc:  # noqa: BLE001
        result = {"passed": False, "reason": reason, "error": str(exc)}

    payload = {**result, "issue_id": issue_id, "goal_id": goal_id}
    if result.get("passed"):
        repo.set_system_state(pool, "last_database_backup", json.dumps(payload, default=str))
    else:
        repo.create_message(
            pool,
            from_team="system",
            to_team="orch-monitor",
            subject=f"Database backup failed after {reason}",
            body=json.dumps(payload, indent=2, default=str),
            priority="high",
            issue_id=issue_id,
            kind="request",
        )
    if issue_id is not None:
        repo.append_log(pool, issue_id, "database_backup", payload)
    return payload
