"""Go stack adapter: go.mod/go.sum projects.

Default workflow to be implemented in a future phase.
"""

from __future__ import annotations

from typing import Any

from . import Adapter


class GoAdapter(Adapter):
    """Adapter for Go projects (detected by go.sum).

    Default steps are not yet implemented.
    """

    def default_steps(self) -> dict[str, list[dict[str, Any]]]:
        """Return default workflow steps for Go projects.

        Returns:
            dict mapping step name to list of action dicts (empty for now).
        """
        return {}
