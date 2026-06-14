"""Request-scoped coordinator selection for the multi-instance dashboard.

The dashboard can front several coordinators (one per Postgres database). A
single FastAPI app serves them all; `?project=<key>` picks which one a request
talks to. The middleware sets `_current` from that query param before the route
runs; route closures use the POOL/SETTINGS/ROSTER proxies below, which transparently
resolve to the current instance — so the 25 route bodies stay coordinator-agnostic.
"""

from __future__ import annotations

import contextvars
from typing import Any, Optional

# Set per-request (in app middleware) to the active Instance; None outside a request.
_current: contextvars.ContextVar = contextvars.ContextVar("current_instance", default=None)

# The Registry, installed once by create_app(). Module-global so templates can read
# the instance list for the dropdown without threading it through every page() call.
_registry: Any = None


def install_registry(registry: Any) -> None:
    global _registry
    _registry = registry


def registry() -> Any:
    return _registry


def set_current(instance: Any) -> contextvars.Token:
    return _current.set(instance)


def reset_current(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> Any:
    inst = _current.get()
    if inst is None:
        raise RuntimeError("no current coordinator in scope (request context not set)")
    return inst


def current_key() -> str:
    inst = _current.get()
    return inst.key if inst is not None else ""


def default_key() -> str:
    return _registry.default_key if _registry is not None else ""


def is_multi() -> bool:
    return _registry is not None and len(_registry.instances) > 1


def show_picker() -> bool:
    """Show the coordinator dropdown when configured via instances.yaml (even for a
    single coordinator) or whenever more than one is registered."""
    if _registry is None:
        return False
    return getattr(_registry, "configured", False) or len(_registry.instances) > 1


def instance_options() -> list[dict[str, Any]]:
    """Rows for the coordinator dropdown: key, label, status, current flag."""
    if _registry is None:
        return []
    cur = current_key()
    return [
        {"key": key, "label": inst.label, "status": _registry.liveness(key),
         "current": key == cur}
        for key, inst in _registry.instances.items()
    ]


class _CurrentProxy:
    """Delegates all attribute access to one attribute of the current Instance,
    resolved per request. Lets `pool`/`settings`/`_roster` stay plain names in the
    route closures while pointing at whichever coordinator the request selected."""

    def __init__(self, attr: str) -> None:
        self._attr = attr

    def _target(self) -> Any:
        return getattr(current(), self._attr)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


# Route closures bind these names; each call resolves to the current instance.
POOL = _CurrentProxy("pool")
SETTINGS = _CurrentProxy("settings")
ROSTER = _CurrentProxy("roster")
