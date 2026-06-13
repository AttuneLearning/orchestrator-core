"""Contract MCP tools — the agent interface to the API contract store.

One row per endpoint, keyed (method, path). The orchestrator's contract_check
gate blocks frontend work until the endpoints it consumes have an *agreed* (or
live) contract; backend agents drive contracts through these tools:

  - contract_propose — a consumer records an endpoint it needs (status 'proposed')
  - contract_agree   — the owner agrees the shape (status 'agreed' → unblocks FE)
  - contract_upsert  — register/seed a contract in full (idempotent; e.g. 'live')
  - contract_get / contract_list — read the store

Thin wrappers over orchestrator.repository (the single write path).
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo


def register(mcp: FastMCP, pool: ConnectionPool) -> None:

    @mcp.tool()
    def contract_propose(method: str, path: str, request_ref: str = "",
                         response_dto: str = "", owner_team: str = "backend",
                         auth: str = "none",
                         source_ref: Optional[str] = None) -> dict[str, Any]:
        """Record a contract a consumer needs (status 'proposed'). No-op if one
        already exists (never downgrades an agreed/live contract)."""
        return repo.propose_contract(pool, method, path, request_ref, response_dto,
                                     owner_team=owner_team, auth=auth,
                                     source_ref=source_ref)

    @mcp.tool()
    def contract_agree(method: str, path: str) -> dict[str, Any]:
        """Owner agrees the endpoint's shape (status 'agreed'). This is the signal
        that unblocks a frontend issue waiting on the contract."""
        return repo.set_contract_status(pool, method, path, "agreed")

    @mcp.tool()
    def contract_upsert(method: str, path: str, request_ref: str = "",
                       response_dto: str = "", auth: str = "none",
                       owner_team: str = "backend", status: str = "proposed",
                       version: str = "1.0",
                       source_ref: Optional[str] = None) -> dict[str, Any]:
        """Insert or fully update a contract (idempotent on method+path). Used to
        register live endpoints / bulk-seed from the API repo assessment."""
        return repo.upsert_contract(pool, method, path, request_ref, response_dto,
                                    auth=auth, owner_team=owner_team, status=status,
                                    version=version, source_ref=source_ref)

    @mcp.tool()
    def contract_get(method: str, path: str) -> Optional[dict[str, Any]]:
        """Fetch a single contract by method + path."""
        return repo.get_contract(pool, method, path)

    @mcp.tool()
    def contract_list(status: Optional[str] = None,
                     owner_team: Optional[str] = None) -> list[dict[str, Any]]:
        """List contracts, optionally filtered by status and/or owner_team."""
        return repo.list_contracts(pool, status=status, owner_team=owner_team)
