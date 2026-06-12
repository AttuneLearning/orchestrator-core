"""Skill MCP tools ported from the agent-workflow skill set.

These are the orchestrator-native counterparts of the slash-command skills
(adr, comms, context, reflect, refine) defined in agent-workflow. They are thin
wrappers over repository.py so both agents and the slash commands share one code
path. memory/adr live in Postgres here rather than the file vault.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo


def register(mcp: FastMCP, pool: ConnectionPool) -> None:

    @mcp.tool()
    def adr_create(domain: str, title: str, decision: str = "",
                   context: str = "") -> dict[str, Any]:
        """Create an ADR (ADR-{DOMAIN}-{NNN}). Mirror of /adr create."""
        return repo.create_adr(pool, domain, title, decision, context)

    @mcp.tool()
    def comms_send(from_team: str, to_team: str, subject: str, body: str = "",
                   priority: str = "medium",
                   issue_id: Optional[int] = None) -> dict[str, Any]:
        """Send a cross-team message. Mirror of /comms send."""
        return repo.create_message(pool, from_team, to_team, subject, body,
                                   priority, issue_id)

    @mcp.tool()
    def comms_check(team: Optional[str] = None) -> list[dict[str, Any]]:
        """List inbound requests awaiting triage. Mirror of /comms check."""
        return repo.pending_messages(pool, to_team=team)

    @mcp.tool()
    def context_load(scope: str = "global", topic: Optional[str] = None,
                     limit: int = 10) -> dict[str, Any]:
        """Load pre-implementation context: recent scoped memory + topic matches.

        Mirror of /context — keeps the response compact (token budget)."""
        recent = [n.body for n in repo.memory_recall(pool, scope=scope, limit=limit)]
        matches = [n.body for n in repo.memory_search(pool, topic, limit=limit)] if topic else []
        return {"scope": scope, "recent": recent, "topic_matches": matches}

    @mcp.tool()
    def reflect(issue_id: int, note: str) -> dict[str, str]:
        """Capture a post-implementation learning as an agent-scoped memory note.

        Mirror of /reflect."""
        repo.memory_write(pool, f"[reflect issue:{issue_id}] {note}", scope="global")
        repo.append_log(pool, issue_id, "reflect", {"note": note})
        return {"status": "ok"}

    @mcp.tool()
    def refine(pattern: str, recommendation: str) -> dict[str, Any]:
        """Record a pattern-refinement recommendation. Mirror of /refine."""
        note = repo.memory_write(
            pool, f"[refine] {pattern}: {recommendation}", scope="global"
        )
        return {"status": "ok", "note_id": note.id}
