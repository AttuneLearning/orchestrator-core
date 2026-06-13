"""Status / alert / suggestion MCP tools for external looping agents.

These let an MCP client (Hermes, OpenClaw, Open Interpreter, …) watch the
orchestrator the way a human watches the dashboard and feed work back in:

  - get_status   — the full fleet rollup (same data as the dashboard `/`)
  - get_alerts   — just the attention set (drifting/quarantined issues, paused
                   goals, stale agents) for a polling loop
  - tail_events  — cursor-based feed over the append-only issue_events log
  - propose_goal — suggest a goal; lands in the gated 'suggested' state, inert
                   until a human promotes it (mirrors adr_create → adr_approve)

Read rollups come from orchestrator.monitoring, shared with the dashboard, so an
agent polling over MCP and a human on the dashboard never disagree.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import monitoring
from .. import repository as repo
from ..config import Settings


def register(mcp: FastMCP, pool: ConnectionPool, settings: Settings) -> None:

    @mcp.tool()
    def get_status() -> dict[str, Any]:
        """Full fleet rollup: goal/issue counts by state, focus %, the attention
        set (flagged + quarantined issues, paused goals), and the agent registry.
        This is the same data the human dashboard renders at `/`."""
        summary = monitoring.fleet_summary(pool, settings)
        summary["agents"] = monitoring.agents_with_staleness(pool)
        summary["suggested_goals"] = [
            asdict(g) for g in repo.list_goals_by_state(pool, "suggested")
        ]
        return summary

    @mcp.tool()
    def get_alerts() -> dict[str, Any]:
        """The attention set only — what needs action right now. Poll this each
        loop: flagged (drifting) and quarantined (off_rails) issues, paused
        goals, and stale (busy-but-silent) agents. below_threshold is the single
        boolean 'is anything wrong' signal."""
        summary = monitoring.fleet_summary(pool, settings)
        stale = [a for a in monitoring.agents_with_staleness(pool) if a["stale"]]
        return {
            "below_threshold": summary["below_threshold"],
            "fleet_focus": summary["fleet_focus"],
            "flagged_issues": summary["flagged_issues"],
            "paused_goals": summary["paused_goals"],
            "stale_agents": stale,
        }

    @mcp.tool()
    def tail_events(after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Cross-issue event feed for progress monitoring, oldest-first.

        Pass after_id=0 first, then the largest `id` you received on each
        subsequent poll to get only new events. Each row carries the global
        `id` (your cursor), issue_id, event_type, state transition, and payload."""
        return [asdict(e) for e in repo.events_since(pool, after_id, limit)]

    @mcp.tool()
    def propose_goal(title: str, description: str = "",
                     pipeline: str = "pipeline-1", suggested_by: str = "agent",
                     source: str = "") -> dict[str, Any]:
        """Suggest a goal for the orchestrator to work on. Mirror of /adr suggest.

        The goal lands in the 'suggested' state — INERT until a human promotes it
        (dashboard, or `cli goal promote <id>`). The engine never decomposes a
        suggested goal, so external agents can propose work without driving it
        unsupervised. suggested_by/source record who proposed it and why."""
        goal = repo.propose_goal(pool, title, description, pipeline,
                                 suggested_by=suggested_by, source=source)
        return asdict(goal)
