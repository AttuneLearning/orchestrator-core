"""Shared pytest fixtures.

Repository tests run against the DATABASE_URL Postgres instance (in-session PG
in the cloud env). Pure-function tests (pipelines, state machine, focus) need no
database and skip these fixtures.
"""

import os

import pytest

from orchestrator import db
from orchestrator.config import load_settings


@pytest.fixture(scope="session")
def settings():
    # Pin the suite to the canonical independent roster (api-repo/web-repo) so
    # tests are hermetic regardless of the operator's ambient ROSTER_FILE choice.
    # Tests that exercise a specific roster set ROSTER_FILE explicitly via
    # monkeypatch.
    os.environ["ROSTER_FILE"] = "config/roster.yaml"
    return load_settings()


@pytest.fixture(scope="session")
def pool(settings):
    if not os.getenv("DATABASE_URL") and "localhost" not in settings.database_url:
        pytest.skip("no DATABASE_URL configured")
    db.migrate(settings)
    p = db.get_pool(settings)
    yield p
    db.close_pool()


@pytest.fixture(autouse=True)
def _clean_db(request):
    """Truncate mutable tables before each DB-backed test."""
    if "pool" not in request.fixturenames:
        return
    p = request.getfixturevalue("pool")
    with p.connection() as conn:
        conn.execute(
            "TRUNCATE goals, issues, issue_events, agents, memory_notes, "
            "messages, adrs, contracts, issue_contract_deps, system_state, "
            "contract_proposals, pending_actions RESTART IDENTITY CASCADE"
        )
