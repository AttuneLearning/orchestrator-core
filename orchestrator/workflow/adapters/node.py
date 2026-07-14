"""Node.js stack adapter: npm-based projects.

Default workflow:
- prepare: reconcile node_modules with package-lock.json
- verify: run typecheck and tests
- cleanup: reset and clean the worktree
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.apply import npm_deps

from . import Adapter


class NodeAdapter(Adapter):
    """Adapter for Node.js projects (detected by package-lock.json)."""

    def default_steps(self) -> dict[str, list[dict[str, Any]]]:
        """Return the standard Node.js workflow steps.

        Prepare installs/reconciles dependencies when package-lock.json changes.
        Verify runs typecheck and unit tests.
        Cleanup resets the worktree.

        Returns:
            dict mapping step name to list of action dicts.
        """
        return {
            "prepare": [
                {
                    "builtin": "node-deps-reconcile",
                    "on_fail": "escalate",
                    "timeout": 300,
                }
            ],
            "verify": [
                {
                    "run": "npm run typecheck && npm test",
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
        """Return builtin action handlers for Node projects.

        node-deps-reconcile: wraps npm_deps.ensure_deps_current to detect
        package-lock.json changes and reinstall node_modules only when needed.

        Returns:
            dict mapping builtin name to handler function.
        """
        return {
            "node-deps-reconcile": self._node_deps_reconcile,
        }

    @staticmethod
    def _node_deps_reconcile(worktree: str | Path) -> dict[str, Any]:
        """Reconcile node_modules with package-lock.json.

        Wraps orchestrator.apply.npm_deps.ensure_deps_current, which detects
        lockfile changes via a sentinel hash and runs npm ci only when needed.

        Args:
            worktree: Path to the Node.js project root.

        Returns:
            dict with at least {ok: bool, reason: str}.
        """
        return npm_deps.ensure_deps_current(worktree)
