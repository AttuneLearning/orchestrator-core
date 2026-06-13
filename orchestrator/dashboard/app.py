"""FastAPI app for the ops dashboard.

create_app(pool, settings) builds the app so tests can inject a pool. All data
access goes through repository.py. The POST routes cover the human review
actions: the slice-B directive functions (repository.apply_directive /
resume_goal) — the only audited way out of the off_rails latch and the
paused-goal state — plus promote/reject for goals externally suggested via MCP.
Read rollups (fleet_summary / agents_with_staleness) are shared with the MCP
status tools via orchestrator.monitoring.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Form
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings, load_settings
from ..db import get_pool
from ..monitoring import agents_with_staleness, fleet_summary
from . import templates


def create_app(pool: Optional[ConnectionPool] = None,
               settings: Optional[Settings] = None,
               reasoner=None) -> FastAPI:
    settings = settings or load_settings()
    pool = pool or get_pool(settings)
    app = FastAPI(title="orchestrator-dashboard")

    from ..pipelines import load_pipelines
    from ..roster import load_roster
    from ..engine.loop import MONITOR_TEAMS
    _pipeline_names = sorted(load_pipelines(settings.pipelines))
    _roster = load_roster(settings.roster)
    # Drafts the suggested reply on the /orch/monitor page. Built once; tests
    # inject a stub. make_reasoner picks the configured backend (e.g. Qwen).
    if reasoner is None:
        from ..agents.reasoning import make_reasoner
        reasoner = make_reasoner(settings)
    _reasoner = reasoner

    def _monitor_pending() -> list:
        # Pending questions for any monitor team, resolving aliases (e.g. 'arch')
        # to the canonical team id so alias-addressed messages still surface.
        out = []
        for m in repo.pending_messages(pool):
            team = _roster.resolve(m["to_team"])
            if team is not None and team.id in MONITOR_TEAMS:
                out.append(m)
        return out

    @app.get("/", response_class=HTMLResponse)
    def overview(added: str = "") -> str:
        summary = fleet_summary(pool, settings)
        summary["suggested_goals"] = [asdict(g) for g in
                                      repo.list_goals_by_state(pool, "suggested")]
        summary["pipelines"] = _pipeline_names
        summary["default_pipeline"] = settings.default_pipeline
        return templates.overview(summary, flash=added)

    @app.post("/goals")
    def add_goal(title: str = Form(...), pipeline: str = Form(""),
                 description: str = Form(""), decompose: str = Form("")):
        from urllib.parse import quote
        title = title.strip()
        if not title:
            return RedirectResponse("/", status_code=303)
        pl = pipeline if pipeline in _pipeline_names else settings.default_pipeline
        mode = decompose if decompose in ("single", "full") else None
        repo.create_goal(pool, title, description.strip(), pipeline=pl, decompose=mode)
        return RedirectResponse(f"/?added={quote(title)}", status_code=303)

    @app.get("/goals/{goal_id}", response_class=HTMLResponse)
    def goal_detail(goal_id: int):
        goal = next((g for g in repo.list_open_goals(pool) if g.id == goal_id), None)
        if goal is None:
            # open-goals list excludes done/paused; fall back to a direct lookup
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT id, title, description, state, pipeline, created_at, "
                    "updated_at FROM goals WHERE id = %s", (goal_id,)
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

    @app.post("/issues/{issue_id}/cancel")
    def cancel_issue(issue_id: int, reason: str = Form("")):
        try:
            repo.cancel_issue(pool, issue_id, reason=reason.strip(), actor="dashboard")
        except ValueError:
            pass  # already terminal — fall through to the detail page
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.get("/agents", response_class=HTMLResponse)
    def agents() -> str:
        return templates.agents_page(agents_with_staleness(pool),
                                     repo.recent_agent_activity(pool, 10))

    @app.get("/adrs", response_class=HTMLResponse)
    def adrs() -> str:
        return templates.adrs_page(repo.list_adrs(pool))

    @app.get("/adrs/{adr_key}", response_class=HTMLResponse)
    def adr_detail(adr_key: str):
        adr = repo.get_adr(pool, adr_key)
        if adr is None:
            return HTMLResponse(templates.page("Not found",
                                f"<h1>No ADR {adr_key}</h1>"), status_code=404)
        from ..adr_rules import reverse_links
        incoming = reverse_links(repo.list_adrs(pool)).get(adr_key, [])
        return templates.adr_detail(adr, incoming)

    @app.post("/adrs/{adr_key}/approve")
    def adr_approve(adr_key: str):
        repo.approve_adr(pool, adr_key, actor="dashboard")
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/update")
    def adr_update(adr_key: str, decision: str = Form(...), context: str = Form("")):
        repo.update_adr(pool, adr_key, decision=decision, context=context)
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/deactivate")
    def adr_deactivate(adr_key: str):
        repo.deactivate_adr(pool, adr_key, actor="dashboard")
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/delete")
    def adr_delete(adr_key: str):
        repo.delete_adr(pool, adr_key)
        return RedirectResponse("/adrs", status_code=303)

    @app.post("/issues/{issue_id}/directive")
    def directive(issue_id: int):
        repo.apply_directive(pool, issue_id, "resume", note="dashboard", actor="dashboard")
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.post("/goals/{goal_id}/resume")
    def resume_goal(goal_id: int):
        repo.resume_goal(pool, goal_id)
        return RedirectResponse(f"/goals/{goal_id}", status_code=303)

    @app.post("/goals/{goal_id}/promote")
    def promote_goal(goal_id: int):
        # Human gate: accept an externally suggested goal into the work queue.
        repo.promote_goal(pool, goal_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/goals/{goal_id}/reject")
    def reject_goal(goal_id: int):
        repo.reject_goal(pool, goal_id)
        return RedirectResponse("/", status_code=303)

    @app.get("/orch/monitor", response_class=HTMLResponse)
    def orch_monitor() -> str:
        # Pending process/architecture questions for the orchestration team. For
        # each, lazily draft a suggested reply (and cache it) for human review.
        messages = _monitor_pending()
        for m in messages:
            if not m.get("draft_response"):
                try:
                    draft = _reasoner.draft_reply(m)
                except Exception as exc:  # noqa: BLE001 - draft is best-effort
                    draft = f"[draft unavailable: {exc}]"
                repo.set_message_draft(pool, m["id"], draft)
                m["draft_response"] = draft
        return templates.orch_monitor(messages)

    @app.post("/orch/monitor/{message_id}/respond")
    def orch_respond(message_id: int, suggested: str = Form(""),
                     override: str = Form("")):
        # Human gate: send the override if provided, else the suggested draft.
        body = override.strip() or suggested.strip()
        if body:
            repo.respond_to_message(pool, message_id, body)
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        summary = fleet_summary(pool, settings)
        summary["agents"] = agents_with_staleness(pool)
        summary["suggested_goals"] = [asdict(g) for g in
                                      repo.list_goals_by_state(pool, "suggested")]
        # jsonable_encoder handles datetimes the stdlib json encoder can't.
        return JSONResponse(jsonable_encoder(summary))

    return app
