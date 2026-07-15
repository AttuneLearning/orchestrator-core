"""Python stack adapter: poetry/uv-based projects.

Default workflow:
- prepare: reconcile virtual environment with uv.lock or poetry.lock
- verify: run unit tests with pytest
- cleanup: reset and clean the worktree
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.workflow import sentinel

from . import Adapter

PYTHON_INSTALL_TIMEOUT_S = 600


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _py_deps_reconcile(worktree: str | Path, action: Any = None) -> dict[str, Any]:
    """Reconcile Python environment with uv.lock or poetry.lock.

    Detects which lockfile exists in the worktree and runs the appropriate
    install command: `uv sync --frozen` for uv.lock, or `poetry install --sync`
    for poetry.lock. Uses a sentinel hash to detect lockfile changes and only
    reinstalls when needed.

    Args:
        worktree: Path to the Python project root.
        action: the driving RequiredAction — ignored (this builtin takes
            no parameters); accepted for parity with the builtin contract
            `fn(worktree, action) -> dict` (runner._run_builtin).

    Returns:
        dict with at least {ok: bool, reason: str}.
    """
    wt = Path(worktree)
    uv_lock = wt / "uv.lock"
    poetry_lock = wt / "poetry.lock"

    # Determine which lockfile exists
    if uv_lock.is_file():
        lock = uv_lock
        cmd = ["uv", "sync", "--frozen"]
        install_type = "uv"
    elif poetry_lock.is_file():
        lock = poetry_lock
        cmd = ["poetry", "install", "--sync"]
        install_type = "poetry"
    else:
        return {
            "checked": False,
            "installed": False,
            "ok": True,
            "reason": "no python lockfile (neither uv.lock nor poetry.lock)",
        }

    want = _hash(lock)

    # Try to get the sentinel location. If we can't resolve the gitdir (not a git repo),
    # treat sentinel as unreadable and skip the optimization.
    sentinel_path = None
    try:
        sentinel_path = sentinel.sentinel_path(wt, "py-lock-hash")
    except ValueError:
        # Not a git repo; skip sentinel optimization and always reinstall when lockfile present
        pass

    have = None

    # Try the sentinel location
    if sentinel_path is not None and sentinel_path.is_file():
        try:
            have = sentinel_path.read_text().strip()
        except OSError:
            pass

    # Check venv directory exists (like node checks node_modules.is_dir())
    venv = wt / ".venv"

    if have == want and venv.is_dir():
        return {
            "checked": True,
            "installed": False,
            "ok": True,
            "reason": f"venv matches {install_type} lockfile",
        }

    # Stale (or first run): install exactly to the lockfile. Never fall back to
    # a non-frozen command that would mutate the lock.
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=PYTHON_INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "checked": True,
            "installed": False,
            "ok": False,
            "reason": f"{install_type} install timed out after {PYTHON_INSTALL_TIMEOUT_S}s",
        }

    if proc.returncode != 0:
        return {
            "checked": True,
            "installed": False,
            "ok": False,
            "reason": f"{install_type} install failed",
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-800:],
        }

    # Record what we installed against so the next call is a cheap hash compare.
    if sentinel_path is not None:
        try:
            sentinel.write_sentinel(wt, "py-lock-hash", want)
        except OSError:
            pass

    return {
        "checked": True,
        "installed": True,
        "ok": True,
        "reason": f"reinstalled: {install_type} lockfile changed since last install",
        "returncode": 0,
        "stderr_tail": proc.stderr[-400:],
    }


class PythonAdapter(Adapter):
    """Adapter for Python projects (detected by poetry.lock or uv.lock).

    Default workflow:
    - prepare: reconcile venv with uv.lock or poetry.lock
    - verify: run pytest -q
    - cleanup: reset and clean the worktree
    """

    def default_steps(self) -> dict[str, list[dict[str, Any]]]:
        """Return the standard Python workflow steps.

        Prepare installs/reconciles dependencies when lockfile changes.
        Verify runs unit tests with pytest.
        Cleanup resets the worktree.

        Returns:
            dict mapping step name to list of action dicts.
        """
        return {
            "prepare": [
                {
                    "builtin": "py-deps-reconcile",
                    "on_fail": "escalate",
                    "timeout": 300,
                }
            ],
            "verify": [
                {
                    "run": "python -m pytest -q",
                    "on_fail": "block",
                    "timeout": 300,
                }
            ],
            "cleanup": [
                {
                    "run": "git reset --hard && git clean -fd",
                    "on_fail": "block",
                    "timeout": 300,
                }
            ],
        }

    def builtins(self) -> dict[str, Any]:
        """Return builtin action handlers for Python projects.

        py-deps-reconcile: reconciles the virtual environment with uv.lock
        or poetry.lock, detecting which exists and running the appropriate
        command, with sentinel-based change detection.

        Also includes the base probe-tcp builtin for service health checks.

        Returns:
            dict mapping builtin name to handler function.
        """
        base_builtins = super().builtins()
        base_builtins["py-deps-reconcile"] = _py_deps_reconcile
        return base_builtins
