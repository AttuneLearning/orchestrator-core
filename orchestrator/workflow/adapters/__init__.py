"""Stack-specific workflow adapters: stack detection and default step definitions.

Each stack (node, python, go, rust) provides:
- default_steps(): dict[str, list[action-dict]] with the standard workflow for that stack
- builtins(): dict[str, callable] mapping builtin action names to handler functions

The loader merges defaults (with auto-detected stack) + repo profile + workspace manifest
into an effective Profile.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any


def _probe_tcp(worktree: str | Path, action: Any) -> dict[str, Any]:
    """Probe a TCP service for reachability.

    Args:
        worktree: checkout root (unused, required by the builtin interface).
        action: the `RequiredAction` driving this probe. `action.args` carries
            the endpoint spec, "service=host:port" or a bare "host:port"
            string (e.g., "mongo=localhost:27017" or "localhost:27017").

    Returns:
        A dict with `ok: bool` and `reason: str`. On success, `ok=True`
        and reason names the endpoint. On failure, `ok=False` and reason
        names the endpoint and why it failed (timeout, connection refused,
        malformed spec, etc). Never raises.
    """
    endpoint_spec = (getattr(action, "args", "") or "").strip()

    # Parse the endpoint, stripping the service name if present
    if "=" in endpoint_spec:
        # Format: "service=host:port"
        _service_name, endpoint = endpoint_spec.split("=", 1)
    else:
        # Format: "host:port"
        endpoint = endpoint_spec

    try:
        host, port_str = endpoint.rsplit(":", 1)
        if not host or not port_str:
            raise ValueError("missing host or port")
        port = int(port_str)
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "reason": f"malformed endpoint {endpoint_spec!r}: {exc}"}

    try:
        with socket.create_connection((host, port), timeout=3):
            return {"ok": True, "reason": endpoint_spec}
    except OSError as exc:
        return {"ok": False, "reason": f"{endpoint_spec}: {exc}"}


def detect_stack(worktree: str | Path) -> str:
    """Detect the runtime stack from lockfile presence.

    Checks for (in order):
    - package-lock.json -> 'node'
    - poetry.lock or uv.lock -> 'python'
    - go.sum -> 'go'
    - Cargo.lock -> 'rust'

    Args:
        worktree: Path to the checkout root.

    Returns:
        Stack string: 'node', 'python', 'go', 'rust', or '' if undetected.
    """
    wt = Path(worktree)

    if (wt / "package-lock.json").is_file():
        return "node"
    if (wt / "poetry.lock").is_file() or (wt / "uv.lock").is_file():
        return "python"
    if (wt / "go.sum").is_file():
        return "go"
    if (wt / "Cargo.lock").is_file():
        return "rust"

    return ""


class Adapter:
    """Base class for stack adapters. Subclasses provide default steps and builtin actions."""

    def default_steps(self) -> dict[str, list[dict[str, Any]]]:
        """Return default workflow steps for this stack.

        Returns a dict mapping step name to a list of action dicts. Each action
        dict has keys: run, builtin, when_changed, sentinel, on_fail, timeout.

        Returns:
            dict[str, list[dict]]: Empty dict by default (override in subclasses).
        """
        return {}

    def builtins(self) -> dict[str, Any]:
        """Return a dict of builtin action handlers.

        Maps builtin action name to a callable `fn(worktree, action) -> dict`
        with at least `{ok: bool, reason: str}`. `action` is the
        `RequiredAction` that resolved to this builtin, so a handler can read
        parameters off `action.args` (e.g. `probe-tcp`'s endpoint spec).

        Returns:
            dict[str, callable]: a plain dict of stack-specific builtins
                (base: just `probe-tcp`; override/extend in subclasses).
        """
        return {"probe-tcp": _probe_tcp}


def get_adapter(stack: str) -> Adapter | None:
    """Get the adapter for a stack type.

    Args:
        stack: Stack string ('node', 'python', 'go', 'rust', or '').

    Returns:
        An Adapter instance if the stack is recognized, else None.
    """
    if stack == "node":
        from . import node
        return node.NodeAdapter()
    elif stack == "python":
        from . import python
        return python.PythonAdapter()
    elif stack == "go":
        from . import golang
        return golang.GoAdapter()
    elif stack == "rust":
        # Rust adapter not yet implemented; return None for now
        return None
    else:
        return None
