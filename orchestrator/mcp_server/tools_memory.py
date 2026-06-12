"""Memory MCP tools — memory_write / memory_recall / memory_search.

Backed by the memory_notes table. Search is LIKE-based for this phase; pgvector
semantic search is a deferred follow-up (the embedding column already exists)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo


def register(mcp: FastMCP, pool: ConnectionPool) -> None:

    @mcp.tool()
    def memory_write(body: str, scope: str = "global") -> dict[str, Any]:
        """Write a scoped memory note (scope: global | pod:* | agent:*)."""
        return asdict(repo.memory_write(pool, body, scope=scope))

    @mcp.tool()
    def memory_recall(scope: str = "global", limit: int = 20) -> list[dict[str, Any]]:
        """Recall the most recent notes for a scope."""
        return [asdict(n) for n in repo.memory_recall(pool, scope=scope, limit=limit)]

    @mcp.tool()
    def memory_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Substring search across all memory notes."""
        return [asdict(n) for n in repo.memory_search(pool, query, limit=limit)]
