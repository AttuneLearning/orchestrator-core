"""Contract MCP tools — the agent interface to the API contract store.

One row per endpoint, keyed (method, path). The orchestrator's contract_check
gate blocks frontend work until the endpoints it consumes have an *agreed* (or
live) contract.

GAP-2 (2026-07-12): the contract store is the SSOT (ADR-DEV-001) and gets the
same protection ADRs got — workers can READ (scoped via contracts_for_issue,
or browse) and PROPOSE (rate-limited), but cannot mutate: contract_agree /
contract_upsert are human-gated (dashboard /contracts, import-contracts CLI)
and are no longer on the worker MCP surface.

Thin wrappers over orchestrator.repository (the single write path).
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo

# "METHOD /path" tokens in issue text, e.g. "GET /clients/:id/notes".
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w:{}./-]*)")


def _require_admin(actor_role: str) -> None:
    """Authorize admin-only contract lifecycle tools.

    ``actor_role`` is captured from the trusted session env in build_server()
    and is never a tool arg, so a worker cannot elevate itself (GAP-2: contract
    mutation is not on the general worker surface)."""
    if (actor_role or "").strip().lower() != "orch-manager":
        raise PermissionError(
            "only the orch-manager session may run contract lifecycle operations"
        )


def register(mcp: FastMCP, pool: ConnectionPool, actor_role: str = "") -> None:

    @mcp.tool()
    def contracts_for_issue(issue_id: int) -> dict[str, Any]:
        """The contracts that govern THIS issue — pull these, NOT the whole store.

        Union of (a) the issue's recorded endpoint dependencies
        (issue_contract_deps) and (b) endpoints named as `METHOD /path` in the
        issue text, each resolved against the contract store. Missing entries are
        listed so a consumer knows what to contract_propose. Use this instead of
        contract_list — it keeps your context small and on-point."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        wanted: list[tuple[str, str]] = []
        for d in repo.list_issue_contract_deps(pool, issue_id):
            wanted.append((d["method"], d["path"]))
        for m, p in _ENDPOINT_RE.findall(f"{issue.title}\n{issue.description or ''}"):
            p = p.rstrip(".,;:")  # sentence punctuation is not part of the path
            if p != "/" and (m, p) not in wanted:
                wanted.append((m, p))
        found, missing = [], []
        for method, path in wanted:
            c = repo.get_contract(pool, method, path)
            (found.append(c) if c else missing.append(f"{method} {path}"))
        return {
            "issue_id": issue_id,
            "contracts": found,
            "missing": missing,
            "note": ("missing endpoints have no contract yet — a consumer should "
                     "contract_propose them; never invent a shape (ADR-DEV-001/002)"),
        }

    @mcp.tool()
    def contract_propose(method: str, path: str, request_ref: str = "",
                         response_dto: str = "", owner_team: str = "backend",
                         auth: str = "none", source_ref: Optional[str] = None,
                         type_ref: Optional[str] = None,
                         proposed_by: str = "agent") -> dict[str, Any]:
        """Record a contract a consumer needs (status 'proposed'). No-op if one
        already exists (never downgrades an agreed/live contract). A human reviews
        and agrees it on the dashboard /contracts page — workers cannot agree or
        upsert contracts directly. Rate-limited per proposer."""
        # GAP-2/G2 loop-breaker: cap proposals per proposer per hour, same guard
        # as adr_suggest (the junk-ADR failure mode, aimed at the contracts SSOT).
        proposer = f"agent:{proposed_by}"
        if repo.recent_contract_proposal_count(pool, proposer, 60) >= 8:
            return {"status": "rate_limited",
                    "message": "too many contract proposals in the last hour — stop "
                               "proposing and work your assigned issue"}
        return repo.propose_contract(pool, method, path, request_ref, response_dto,
                                     owner_team=owner_team, auth=auth,
                                     source_ref=source_ref, type_ref=type_ref,
                                     proposed_by=proposer)

    @mcp.tool()
    def contract_get(method: str, path: str) -> Optional[dict[str, Any]]:
        """Fetch a single contract by method + path."""
        return repo.get_contract(pool, method, path)

    @mcp.tool()
    def contract_list(status: Optional[str] = None,
                     owner_team: Optional[str] = None) -> list[dict[str, Any]]:
        """Browse contracts (read-only), optionally by status/owner_team. For the
        contracts that apply to YOUR issue use contracts_for_issue(issue_id) —
        it is scoped and far cheaper than pulling the whole store."""
        return repo.list_contracts(pool, status=status, owner_team=owner_team)

    @mcp.tool()
    def contract_lifecycle_preview(
        project: str, operation_id: str, reason: str,
        changes: list[dict[str, Any]],
        expected: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """ADMIN ONLY. Dry-run a contract lifecycle batch: validation conflicts,
        advisory warnings, affected contracts, whether confirmation is required."""
        _require_admin(actor_role)
        return repo.contract_lifecycle_preview(
            pool, project, operation_id,
            actor=os.environ.get("ORCH_ACTOR", "orch-manager"),
            actor_role=actor_role, reason=reason, changes=changes, expected=expected)

    @mcp.tool()
    def contract_lifecycle_apply(
        project: str, operation_id: str, reason: str,
        changes: list[dict[str, Any]],
        expected: Optional[dict[str, str]] = None,
        preview_token: Optional[str] = None,
        confirm_project: Optional[str] = None,
    ) -> dict[str, Any]:
        """ADMIN ONLY. Atomically apply a lifecycle batch (idempotent by
        operation_id). Destructive batches (supersede/retire, or deprecate of an
        agreed/live contract) require confirm_project == the configured project name,
        else result='rejected' reason='confirmation_required'."""
        _require_admin(actor_role)
        return repo.contract_lifecycle_apply(
            pool, project, operation_id,
            actor=os.environ.get("ORCH_ACTOR", "orch-manager"),
            actor_role=actor_role, reason=reason, changes=changes, expected=expected,
            source="mcp", confirm_project=confirm_project)

    @mcp.tool()
    def contract_lifecycle_history(
        contract_id: Optional[int] = None, operation_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """ADMIN ONLY. Read the append-only lifecycle history for a contract and/or
        operation id."""
        _require_admin(actor_role)
        return repo.contract_lifecycle_history(
            pool, contract_id=contract_id, operation_id=operation_id)
