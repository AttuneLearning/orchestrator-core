"""Shared read-only monitoring rollups.

The fleet summary and agent-staleness view are consumed by BOTH the FastAPI
dashboard (dashboard/app.py) and the MCP status tools (mcp_server/tools_status.py)
so a human watching the dashboard and an external looping agent polling over MCP
always see the same attention set. Drift detection reuses
focus.signals_after_directive — the exact predicate the engine sweep uses to
quarantine — so monitoring and the engine never disagree about what is a concern.

Read-only: imports repository + engine.focus only, no I/O of its own.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from psycopg_pool import ConnectionPool

from . import repository as repo
from .config import Settings
from .engine import focus

# Active = issues the engine is still working; excludes done/failed/off_rails.
ACTIVE_STATES = ["backlog", "ready", "in_progress", "in_review", "blocked"]
STALE_AFTER_S = 600  # a busy agent silent this long is flagged stale


def agents_with_staleness(pool: ConnectionPool) -> list[dict[str, Any]]:
    """Agent registry with a computed `stale` flag (busy + heartbeat too old)."""
    out = []
    now = datetime.now(timezone.utc)
    for a in repo.list_agents(pool):
        d = asdict(a)
        d["stale"] = bool(
            a.status == "busy" and a.last_seen is not None
            and (now - a.last_seen).total_seconds() > STALE_AFTER_S
        )
        out.append(d)
    return out


def fleet_summary(pool: ConnectionPool, settings: Settings) -> dict[str, Any]:
    """Compose the fleet overview: state counts, the attention set, and focus %.

    The attention set is what a human (or monitoring agent) must act on:
      - active issues tripping a mechanical signal (drifting, not yet quarantined)
      - off_rails issues (already quarantined — awaiting a directive)
      - paused goals (awaiting a resume)
    fleet_focus counts both active and quarantined issues, so a quarantine
    visibly drops the score.
    """
    goals_list = [
        {**asdict(g), "issue_count": repo.count_issues_for_goal(pool, g.id)}
        for g in repo.list_all_goals(pool)
    ]
    active = repo.list_issues(pool, states=ACTIVE_STATES)
    flagged: list[dict[str, Any]] = []
    for issue in active:
        events = repo.recent_events(pool, issue.id, limit=200)
        signals = focus.signals_after_directive(issue, events, settings.thresholds)
        if signals:
            flagged.append({"id": issue.id, "title": issue.title,
                            "state": issue.state, "signals": signals})
    quarantined = [
        {"id": i.id, "title": i.title, "state": i.state, "signals": ["off_rails"]}
        for i in repo.list_issues(pool, states=["off_rails"])
    ]
    paused_goals = [g for g in goals_list if g["state"] == "paused"]
    # Terminal-failed issues: the engine won't re-drive them (retry cap exhausted),
    # so they need a human directive (re-open) or cancel — surface them for action.
    failed_issues = [
        {"id": i.id, "title": i.title, "state": i.state, "retry_count": i.retry_count,
         # A decomposed parent (epic) has no gate; retrying it is a no-op. Flag it so
         # the dashboard hides the retry button and points at the failed child instead.
         "has_children": bool(repo.list_issues(pool, parent_id=i.id))}
        for i in repo.list_issues(pool, states=["failed"])
    ]

    attention = flagged + quarantined
    denom = len(active) + len(quarantined)
    return {
        "goals": repo.count_by_state(pool, "goals"),
        "issues": repo.count_by_state(pool, "issues"),
        "active_issues": denom,
        "flagged": len(attention),
        "fleet_focus": focus.fleet_focus(denom, len(attention)),
        "below_threshold": bool(attention or paused_goals),
        "flagged_issues": attention,
        "failed_issues": failed_issues,
        "paused_goals": paused_goals,
        "goals_list": goals_list,
    }
