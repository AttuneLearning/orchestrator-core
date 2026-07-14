"""Stack-specific workflow adapters: stack detection and default step definitions.

Each stack (node, python, go, rust) provides:
- default_steps(): dict[str, list[action-dict]] with the standard workflow for that stack
- builtins(): dict[str, callable] mapping builtin action names to handler functions

The loader merges defaults (with auto-detected stack) + repo profile + workspace manifest
into an effective Profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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

        Maps builtin action name to a callable fn(worktree) -> dict with
        at least {ok: bool, reason: str}.

        Returns:
            dict[str, callable]: Empty dict by default (override in subclasses).
        """
        return {}


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
