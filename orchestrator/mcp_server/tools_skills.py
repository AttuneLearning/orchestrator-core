"""Skill MCP tools ported from the upstream agent-workflow skill set
(github.com/AttuneLearning/agent-workflow @ 555ff00).

These are the orchestrator-native counterparts of the slash-command skills
(adr, comms, context, reflect, refine) defined in agent-workflow. They are thin
wrappers over repository.py so both agents and the slash commands share one code
path. memory/adr live in Postgres here rather than the file vault.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import adr_rules
from .. import repository as repo


def _adr_tokens(text: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
            if len(w) > 2}


def _duplicate_adr(existing: list[dict[str, Any]], domain: str, title: str,
                   decision: str) -> Optional[str]:
    """adr_key of an accepted/proposed rule equivalent to the proposed one, else
    None. Equivalence = same domain AND (near-identical title OR high decision
    token-overlap). Deterministic, no LLM — the orch-manager's auto-reject test."""
    nt, nd = _adr_tokens(title), _adr_tokens(decision)
    for a in existing:
        if a.get("status") not in ("accepted", "proposed"):
            continue
        if (a.get("domain") or "").upper() != (domain or "").upper():
            continue
        at, ad = _adr_tokens(a.get("title", "")), _adr_tokens(a.get("decision", ""))
        title_j = len(nt & at) / max(1, len(nt | at))
        dec_j = len(nd & ad) / max(1, len(nd | ad))
        if title_j >= 0.6 or dec_j >= 0.55:
            return a["adr_key"]
    return None


def register(mcp: FastMCP, pool: ConnectionPool) -> None:

    @mcp.tool()
    def adr_for_issue(issue_id: int) -> dict[str, Any]:
        """The ADRs that govern THIS issue — pull these, NOT the whole catalog.

        Returns only the accepted rules related to the issue: the reasoner's tags
        UNION the team/work-type selector matches, expanded via the full backlink
        closure (uncapped — nothing relevant is dropped). Call this each cycle in
        place of adr_list; it keeps your context small and on-point. `rules_block`
        is the ready-to-honor directive text (a gate review cites these ids)."""
        rules = repo.adrs_for_issue(pool, issue_id)
        return {
            "issue_id": issue_id,
            "count": len(rules),
            "adrs": [{"adr_key": r["adr_key"], "decision": r["decision"],
                      "domain": r["domain"]} for r in rules],
            "rules_block": adr_rules.format_rules_block(rules),
        }

    @mcp.tool()
    def adr_suggest(domain: str, title: str, decision: str, context: str = "",
                    work_types: Optional[list[str]] = None,
                    teams: Optional[list[str]] = None,
                    repos: Optional[list[str]] = None,
                    proposed_by: str = "agent",
                    issue_id: Optional[int] = None) -> dict[str, Any]:
        """Suggest a NEW architecture rule to the orch-manager. Workers CANNOT
        create or approve ADRs directly — you suggest, a human decides.

        Duplicates are auto-rejected (same domain + near-identical title/decision).
        A novel suggestion is filed as a 'proposed' ADR (inert) AND flagged to the
        human manager: it appears on the dashboard /adrs review queue and in the
        orchestration inbox shown by `status`. Returns status 'duplicate' or
        'suggested'."""
        proposer = f"agent-suggest:{proposed_by}"
        # G2 loop-breaker: a wedged worker cannot spam governance. Cap suggestions
        # per proposer per hour; over the cap, refuse and tell it to do its work.
        if repo.recent_adr_proposal_count(pool, proposer, 60) >= 5:
            return {"status": "rate_limited",
                    "message": "too many ADR suggestions in the last hour — stop "
                               "suggesting and work your assigned issue"}
        dup = _duplicate_adr(repo.list_adrs(pool), domain, title, decision)
        if dup:
            return {"status": "duplicate", "of": dup,
                    "message": "an equivalent rule already exists or is pending — not filed"}
        adr = repo.create_adr(
            pool, domain, title, decision, context,
            applies_to={"work_types": work_types or [], "teams": teams or [],
                        "repos": repos or []},
            status="proposed", proposed_by=proposer,
        )
        # 'orchestration'-team messages are never auto-decomposed (loop.MONITOR_TEAMS):
        # they stay pending for the human, surfacing on /orch/monitor and in `status`.
        repo.create_message(
            pool, from_team=(teams[0] if teams else "agent"), to_team="orchestration",
            subject=f"ADR suggestion {adr['adr_key']}: {title}",
            body=(f"decision: {decision}\ncontext: {context}\n"
                  f"Review + approve/trash on the dashboard /adrs queue."),
            priority="high", issue_id=issue_id,
        )
        return {"status": "suggested", "adr_key": adr["adr_key"],
                "note": "filed as proposed + flagged to the human manager for review"}

    @mcp.tool()
    def adr_list(status: Optional[str] = None,
                 domain: Optional[str] = None) -> list[dict[str, Any]]:
        """Browse ADR rules (read-only), optionally by status/domain. For the rules
        that apply to YOUR issue use adr_for_issue(issue_id) instead of this — it is
        scoped and far cheaper than pulling the whole catalog."""
        return repo.list_adrs(pool, status=status, domain=domain)

    @mcp.tool()
    def adr_get(adr_key: str) -> Optional[dict[str, Any]]:
        """Fetch one ADR rule by key. Mirror of /adr review."""
        return repo.get_adr(pool, adr_key)

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
        # Keep topic matches within the same scope so context_load stays consistent
        # (and never leaks the reserved monitor:* namespace into another scope's load).
        matches = ([n.body for n in repo.memory_search(pool, topic, limit=limit, scope=scope)]
                   if topic else [])
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
