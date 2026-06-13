"""FastMCP server exposing issue, memory, and skill tools over stdio.

Run via `python -m orchestrator.cli serve`. Tools are registered from the
sibling modules; all of them operate through the shared connection pool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..config import load_settings
from ..db import get_pool
from . import (
    tools_contracts,
    tools_issues,
    tools_memory,
    tools_skills,
    tools_status,
)


def build_server() -> FastMCP:
    settings = load_settings()
    pool = get_pool(settings)
    mcp = FastMCP("orchestrator")
    tools_issues.register(mcp, pool)
    tools_memory.register(mcp, pool)
    tools_skills.register(mcp, pool)
    tools_status.register(mcp, pool, settings)
    tools_contracts.register(mcp, pool)
    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
