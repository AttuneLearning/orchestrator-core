"""Docs MCP tools — doc_write / doc_get / doc_list / doc_search / doc_delete.

Shared cross-agent development docs, backed by the `docs` table (migration 0018).
This is the canonical store agents read/write instead of filesystem docs (per
ADR-ORCH-008): one DB-backed copy reachable identically from every worktree, so
there is no drift and no per-worktree duplication. Same posture as the adr_* /
memory_* / contract_* tools.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo


def register(mcp: FastMCP, pool: ConnectionPool) -> None:

    @mcp.tool()
    def doc_write(path: str, body: str, title: str = "",
                  format: str = "markdown", author: str = "") -> dict[str, Any]:
        """Create or update a shared dev doc at `path` (e.g.
        'architecture/knowledge-packets'). Overwrites if it exists. `format` is
        'markdown' | 'html' | 'text'. Use this instead of writing doc files into
        your worktree — the doc lives in the orchestrator DB and is visible to all
        agents and on the dashboard Docs tab."""
        return repo.doc_upsert(pool, path, title=title or path.split("/")[-1],
                               body=body, format=format, author=author)

    @mcp.tool()
    def doc_get(path: str) -> Optional[dict[str, Any]]:
        """Fetch one shared dev doc (full body) by path."""
        return repo.doc_get(pool, path)

    @mcp.tool()
    def doc_list() -> list[dict[str, Any]]:
        """List all shared dev docs (metadata, newest-first; no bodies)."""
        return repo.doc_list(pool)

    @mcp.tool()
    def doc_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search shared dev docs by term over title + body."""
        return repo.doc_search(pool, query, limit=limit)

    @mcp.tool()
    def doc_delete(path: str) -> dict[str, Any]:
        """Delete a shared dev doc by path."""
        return {"deleted": repo.doc_delete(pool, path), "path": path}
