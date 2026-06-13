"""Skill MCP tools ported from the upstream agent-workflow skill set
(github.com/AttuneLearning/agent-workflow @ 555ff00).

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
                   context: str = "",
                   work_types: Optional[list[str]] = None,
                   teams: Optional[list[str]] = None,
                   repos: Optional[list[str]] = None,
                   related: Optional[list[str]] = None,
                   supersedes: Optional[list[str]] = None,
                   patterns: Optional[list[str]] = None,
                   proposed_by: str = "agent") -> dict[str, Any]:
        """Propose an ADR rule (ADR-{DOMAIN}-{NNN}). Mirror of /adr suggest.

        decision = one compact imperative directive agents will receive.
        Empty selector dimensions match everything; repos=[] = project-wide.
        Proposals are inert until a human approves (adr_approve)."""
        return repo.create_adr(
            pool, domain, title, decision, context,
            applies_to={"work_types": work_types or [], "teams": teams or [],
                        "repos": repos or []},
            related=related, supersedes=supersedes, patterns=patterns,
            status="proposed", proposed_by=proposed_by,
        )

    @mcp.tool()
    def adr_list(status: Optional[str] = None,
                 domain: Optional[str] = None) -> list[dict[str, Any]]:
        """List ADR rules, optionally by status/domain. Mirror of /adr status."""
        return repo.list_adrs(pool, status=status, domain=domain)

    @mcp.tool()
    def adr_get(adr_key: str) -> Optional[dict[str, Any]]:
        """Fetch one ADR rule by key. Mirror of /adr review."""
        return repo.get_adr(pool, adr_key)

    @mcp.tool()
    def adr_approve(adr_key: str, actor: str = "human") -> dict[str, Any]:
        """Promote a proposed ADR to accepted (it becomes live for agents)."""
        return repo.approve_adr(pool, adr_key, actor=actor)

    @mcp.tool()
    def adr_update(adr_key: str, title: Optional[str] = None,
                   decision: Optional[str] = None, context: Optional[str] = None,
                   work_types: Optional[list[str]] = None,
                   teams: Optional[list[str]] = None,
                   repos: Optional[list[str]] = None) -> dict[str, Any]:
        """Edit an ADR's content (decision/context/title/selectors). Only provided
        fields change; status is preserved (an accepted rule stays live with the
        corrected text). This is the single-source-of-truth edit path."""
        applies_to = None
        if work_types is not None or teams is not None or repos is not None:
            applies_to = {"work_types": work_types or [], "teams": teams or [],
                          "repos": repos or []}
        return repo.update_adr(pool, adr_key, title=title, decision=decision,
                               context=context, applies_to=applies_to)

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
    def comms_read(team: Optional[str] = None) -> list[dict[str, Any]]:
        """List inbound responses (answers) addressed to a team, newest first.
        The read side of comms_send: how a worker consumes a reply to a question
        it (or its team) raised."""
        return repo.list_responses(pool, to_team=team)

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
