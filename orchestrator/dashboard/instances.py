"""Coordinator registry for the multi-instance dashboard.

Loads config/instances.yaml into a set of Instances (one per Postgres database),
each carrying its own Settings (DATABASE_URL + roster) and a lazily-opened pool.
Liveness comes from each DB's own daemon heartbeat (server clock), cached briefly
so rendering the dropdown never hammers — or hangs on — a slow/dead coordinator.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any, Optional

from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import CONFIG_DIR, REPO_ROOT, Settings, _yaml, load_settings
from ..roster import Roster, load_roster

# A daemon that ticked within this many seconds is "live"; older but reachable is
# "idle"; an unreachable DB is "down".
_LIVE_WINDOW_SECONDS = 30.0
_LIVENESS_TTL_SECONDS = 5.0


class Instance:
    """One coordinator: a label, its Settings, and a lazily-opened connection pool."""

    def __init__(self, key: str, label: str, settings: Settings,
                 pool: Optional[ConnectionPool] = None) -> None:
        self.key = key
        self.label = label
        self.settings = settings
        self.roster: Roster = load_roster(settings.roster)
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            # Short connect timeout + min_size=0 so a dead coordinator degrades
            # gracefully (marked "down" in the dropdown) instead of stalling.
            self._pool = ConnectionPool(
                self.settings.database_url, min_size=0, max_size=4, open=True,
                kwargs={"connect_timeout": 3},
            )
        return self._pool


class Registry:
    def __init__(self, instances: dict[str, Instance], default_key: str,
                 configured: bool = False) -> None:
        self.instances = instances
        self.default_key = default_key
        # True when coordinators came from instances.yaml — show the picker even
        # for a single one (so its liveness is visible); False for the injected /
        # fallback 'default' (tests behave exactly as the single-coordinator app).
        self.configured = configured
        self._live_cache: dict[str, tuple[float, str]] = {}

    def get(self, key: Optional[str]) -> Instance:
        return self.instances.get(key or "", self.instances[self.default_key])

    def resolve_key(self, raw: Optional[str]) -> str:
        return raw if raw in self.instances else self.default_key

    def liveness(self, key: str) -> str:
        now = time.monotonic()
        cached = self._live_cache.get(key)
        if cached and now - cached[0] < _LIVENESS_TTL_SECONDS:
            return cached[1]
        status = self._probe(key)
        self._live_cache[key] = (now, status)
        return status

    def _probe(self, key: str) -> str:
        inst = self.instances.get(key)
        if inst is None:
            return "down"
        try:
            age = repo.daemon_heartbeat_age_seconds(inst.pool)
        except Exception:  # noqa: BLE001 - unreachable DB / bad creds → down
            return "down"
        if age is None:
            return "idle"  # DB reachable, daemon has never ticked
        return "live" if age < _LIVE_WINDOW_SECONDS else "idle"


def _single(settings: Optional[Settings] = None,
            pool: Optional[ConnectionPool] = None) -> Registry:
    """Fallback / injected-pool path: one 'default' coordinator (current behavior)."""
    settings = settings or load_settings()
    inst = Instance("default", "Default", settings, pool=pool)
    return Registry({"default": inst}, "default")


def load_registry(settings: Optional[Settings] = None,
                  pool: Optional[ConnectionPool] = None) -> Registry:
    """Build the coordinator registry. With an injected pool (tests) or no
    instances.yaml, returns a single 'default' coordinator — identical to the
    pre-multi-instance dashboard."""
    if pool is not None:
        return _single(settings, pool)
    path = CONFIG_DIR / "instances.yaml"
    if not path.exists():
        return _single(settings)
    data = _yaml(path)
    base = settings or load_settings()
    instances: dict[str, Instance] = {}
    for key, spec in (data.get("instances") or {}).items():
        spec = spec or {}
        db = os.getenv(spec["database_url_env"]) if spec.get("database_url_env") \
            else spec.get("database_url")
        if not db:
            continue  # referenced env var unset → skip (don't guess credentials)
        roster_file = spec.get("roster_file", base.roster_file)
        s = dataclasses.replace(
            base, database_url=db, roster_file=roster_file,
            roster=_yaml(REPO_ROOT / roster_file),
        )
        instances[key] = Instance(key, spec.get("label", key), s)
    if not instances:
        return _single(base)
    default_key = data.get("default")
    if default_key not in instances:
        default_key = next(iter(instances))
    return Registry(instances, default_key, configured=True)
