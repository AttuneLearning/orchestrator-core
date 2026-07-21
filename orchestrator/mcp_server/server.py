"""FastMCP server exposing issue, memory, and skill tools over stdio.

Run via `python -m orchestrator.cli serve`. Tools are registered from the
sibling modules; all of them operate through the shared connection pool.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from ..config import load_settings
from ..db import get_pool
from . import (
    tools_contracts,
    tools_docs,
    tools_issues,
    tools_memory,
    tools_skills,
    tools_status,
)


def build_server() -> FastMCP:
    settings = load_settings()
    pool = get_pool(settings)
    mcp = FastMCP("orchestrator")
    # Silence the low-level server's per-request INFO spam ("Processing request of
    # type CallToolRequest") — it floods every worker's tmux pane / log with no signal.
    # Set at the logger level so it holds regardless of FastMCP's root logging config.
    import logging
    logging.getLogger("mcp").setLevel(logging.WARNING)
    # The launcher starts one stdio MCP server per agent session and supplies
    # the trusted session role.  Do not accept a role as a tool argument: that
    # would let any caller self-identify as the coordinator.  Fail closed when
    # a runtime omitted the identity.
    server_role = os.environ.get("ORCH_ROLE", "").strip().lower()
    tools_issues.register(mcp, pool, settings, actor_role=server_role)
    tools_memory.register(mcp, pool)
    tools_skills.register(mcp, pool)
    tools_status.register(mcp, pool, settings)
    tools_contracts.register(mcp, pool, actor_role=server_role)
    tools_docs.register(mcp, pool)
    # Zero-touch grounding: build the orch-monitor KB on first connect if empty.
    try:
        from ..monitor_kb import bootstrap_monitor_kb
        bootstrap_monitor_kb(pool, settings)
    except Exception:  # noqa: BLE001 - KB bootstrap must never block the server
        pass
    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
