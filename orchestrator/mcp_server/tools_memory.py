"""Memory MCP tools — memory_write / memory_recall / memory_search.

Backed by the memory_notes table.  When an embedder is configured (provider !=
'none') write and search both use semantic embeddings stored in the
embedding_v vector(256) column (slice H).  Gracefully degrades to ILIKE-only
search when pgvector is unavailable or the provider is 'none'."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings
from ..embeddings import Embedder, make_embedder


def register(mcp: FastMCP, pool: ConnectionPool, settings: Optional[Settings] = None) -> None:
    # Resolve embedder once at registration time.
    embedder: Optional[Embedder] = make_embedder(settings) if settings is not None else None

    @mcp.tool()
    def memory_write(body: str, scope: str = "global") -> dict[str, Any]:
        """Write a scoped memory note (scope: global | pod:* | agent:*)."""
        embedding = embedder.embed(body) if embedder is not None else None
        return asdict(repo.memory_write(pool, body, scope=scope, embedding=embedding))

    @mcp.tool()
    def memory_recall(scope: str = "global", limit: int = 20) -> list[dict[str, Any]]:
        """Recall the most recent notes for a scope."""
        return [asdict(n) for n in repo.memory_recall(pool, scope=scope, limit=limit)]

    @mcp.tool()
    def memory_search(query: str, limit: int = 20,
                      scope: Optional[str] = None) -> list[dict[str, Any]]:
        """Semantic + substring search. With `scope`, restrict to that scope;
        without it, the reserved private 'monitor:*' namespace is excluded."""
        query_embedding = embedder.embed(query) if embedder is not None else None
        return [asdict(n) for n in repo.memory_search(
            pool, query, limit=limit, query_embedding=query_embedding, scope=scope
        )]
