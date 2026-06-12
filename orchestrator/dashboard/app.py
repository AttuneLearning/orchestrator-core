"""FastAPI app for the ops dashboard.

create_app(pool, settings) builds the app so tests can inject a pool. All data
access goes through repository.py; the two POST routes call the slice-B directive
functions (repository.apply_directive / resume_goal), which are the only audited
way out of the off_rails latch and the paused-goal state.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings, load_settings
from ..db import get_pool
from ..engine import focus
from . import templates

_ACTIVE_STATES = ["backlog", "ready", "in_progress", "in_review", "blocked"]


def _fleet_summary(pool: ConnectionPool, settings: Settings) -> dict[str, Any]:
    """Compose the fleet overview: state counts, the attention set, and focus %.

    The attention set is what a human must act on:
      - active issues tripping a mechanical signal (drifting, not yet quarantined)
      - off_rails issues (already quarantined — awaiting a directive)
      - paused goals (awaiting a resume)
    Drift detection reuses focus.signals_after_directive — the exact predicate the
    engine sweep uses to quarantine — so the dashboard and the engine never
    disagree about which active issues are a concern. fleet_focus counts both
    active and quarantined issues, so a quarantine visibly drops the score.
    """
    goals_list = [
        {**asdict(g), "issue_count": repo.count_issues_for_goal(pool, g.id)}
        for g in repo.list_all_goals(pool)
    ]
    active = repo.list_issues(pool, states=_ACTIVE_STATES)
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
        "paused_goals": paused_goals,
        "goals_list": goals_list,
    }


def create_app(pool: Optional[ConnectionPool] = None,
               settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    pool = pool or get_pool(settings)
    app = FastAPI(title="orchestrator-dashboard")

    @app.get("/", response_class=HTMLResponse)
    def overview() -> str:
        return templates.overview(_fleet_summary(pool, settings))

    @app.get("/goals/{goal_id}", response_class=HTMLResponse)
    def goal_detail(goal_id: int):
        goal = next((g for g in repo.list_open_goals(pool) if g.id == goal_id), None)
        if goal is None:
            # open-goals list excludes done/paused; fall back to a direct lookup
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT id, title, description, state, created_at, updated_at "
                    "FROM goals WHERE id = %s", (goal_id,)
                ).fetchone()
            if row is None:
                return HTMLResponse(templates.page("Not found",
                                    f"<h1>No goal #{goal_id}</h1>"), status_code=404)
            from ..models import Goal
            goal = Goal(*row)
        issues = [asdict(i) for i in repo.issue_tree(pool, goal_id)]
        return templates.goal_detail(asdict(goal), issues)

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_detail(issue_id: int):
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            return HTMLResponse(templates.page("Not found",
                                f"<h1>No issue #{issue_id}</h1>"), status_code=404)
        events = [asdict(e) for e in repo.issue_timeline(pool, issue_id)]
        return templates.issue_detail(asdict(issue), events)

    @app.get("/agents", response_class=HTMLResponse)
    def agents() -> str:
        return templates.agents_page([asdict(a) for a in repo.list_agents(pool)])

    @app.post("/issues/{issue_id}/directive")
    def directive(issue_id: int):
        repo.apply_directive(pool, issue_id, "resume", note="dashboard", actor="dashboard")
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.post("/goals/{goal_id}/resume")
    def resume_goal(goal_id: int):
        repo.resume_goal(pool, goal_id)
        return RedirectResponse(f"/goals/{goal_id}", status_code=303)

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        summary = _fleet_summary(pool, settings)
        summary["agents"] = [asdict(a) for a in repo.list_agents(pool)]
        # jsonable_encoder handles datetimes the stdlib json encoder can't.
        return JSONResponse(jsonable_encoder(summary))

    return app
