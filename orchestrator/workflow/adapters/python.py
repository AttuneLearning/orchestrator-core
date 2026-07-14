"""Python stack adapter: poetry/uv-based projects.

Default workflow to be implemented in WP-21.
"""

from __future__ import annotations

from typing import Any

from . import Adapter


class PythonAdapter(Adapter):
    """Adapter for Python projects (detected by poetry.lock or uv.lock).

    Default steps (prepare, verify, cleanup) are filled in WP-21.
    """

    def default_steps(self) -> dict[str, list[dict[str, Any]]]:
        """Return default workflow steps for Python projects.

        Returns:
            dict mapping step name to list of action dicts (empty for now).
        """
        return {}
