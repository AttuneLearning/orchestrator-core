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
    from . import context
    from .instances import load_registry
    # One dashboard, many coordinators (one per DB). The registry routes each
    # request to the coordinator named by ?project=; an injected pool / no
    # instances.yaml collapses to a single 'default' coordinator (current behavior).
    registry = load_registry(settings=settings, pool=pool)
    context.install_registry(registry)
    base_settings = registry.get(registry.default_key).settings
    app = FastAPI(title="orchestrator-dashboard")

    # Request-scoped proxies — these resolve to whichever coordinator ?project=
    # selected, so the route bodies below stay coordinator-agnostic.
    pool = context.POOL
    settings = context.SETTINGS
    _roster = context.ROSTER

    from ..pipelines import load_pipelines
    from ..engine.loop import MONITOR_TEAMS
    # Drafts the suggested reply on the /orch/monitor page. Built once from the
    # default coordinator's settings; tests inject a stub. make_reasoner picks the
    # configured backend (e.g. Qwen).
    if reasoner is None:
        from ..agents.reasoning import make_reasoner
        reasoner = make_reasoner(base_settings)
    _reasoner = reasoner
    from ..monitor_kb import retrieve_context

    def _pipelines() -> list:
        return sorted(load_pipelines(settings.pipelines))

    @app.middleware("http")
    async def _coordinator_scope(request, call_next):
        key = registry.resolve_key(request.query_params.get("project"))
        token = context.set_current(registry.get(key))
        try:
            response = await call_next(request)
        finally:
            context.reset_current(token)
        # Preserve the coordinator across redirects: a write must return to the
        # same DB its form was submitted against (default stays on clean URLs).
        if response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("location", "")
            if (loc.startswith("/") and "://" not in loc
                    and "project=" not in loc and key != registry.default_key):
                sep = "&" if "?" in loc else "?"
                response.headers["location"] = f"{loc}{sep}project={key}"
        return response

    def _monitor_pending() -> list:
        # Pending questions for any monitor team, resolving aliases (e.g. 'orch-monitor')
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
        summary["pipelines"] = _pipelines()
        summary["default_pipeline"] = settings.default_pipeline
        # Orchestrator-queue alert badge + recent correspondence for the side panel.
        summary["open_monitor_msgs"] = len(_monitor_pending())
        summary["recent_messages"] = repo.list_messages(pool, limit=8)
        return templates.overview(summary, flash=added)

    @app.post("/goals")
    def add_goal(title: str = Form(...), pipeline: str = Form(""),
                 description: str = Form(""), decompose: str = Form("")):
        from urllib.parse import quote
        title = title.strip()
        if not title:
            return RedirectResponse("/", status_code=303)
        pl = pipeline if pipeline in _pipelines() else settings.default_pipeline
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

    @app.post("/goals/{goal_id}/complete")
    def complete_goal(goal_id: int):
        # Human verdict: the work is actually done (or the goal is being retired).
        repo.complete_goal(pool, goal_id)
        return RedirectResponse("/", status_code=303)

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
                # Ground the draft in the isolated monitor KB (keyword-overlap retrieval).
                q = f"{m['subject']}\n{m.get('body', '')}"
                context = "\n\n".join(retrieve_context(pool, q, limit=8))
                try:
                    # Pass 1: draft grounded in the KB. Pass 2: QA cross-check the
                    # draft against the same reference (corrects guesses/inaccuracies).
                    draft = _reasoner.draft_reply(m, context=context)
                    review = getattr(_reasoner, "review_reply", None)
                    if review is not None:
                        draft = review(m, context=context, draft=draft)
                except Exception as exc:  # noqa: BLE001 - draft is best-effort
                    draft = f"[draft unavailable: {exc}]"
                repo.set_message_draft(pool, m["id"], draft)
                m["draft_response"] = draft
        history = repo.list_messages(pool, limit=30)
        return templates.orch_monitor(messages, history=history)

    @app.post("/orch/monitor/{message_id}/respond")
    def orch_respond(message_id: int, suggested: str = Form(""),
                     override: str = Form("")):
        # Human gate: send the override if provided, else the suggested draft.
        body = override.strip() or suggested.strip()
        if body:
            repo.respond_to_message(pool, message_id, body)
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.get("/contracts", response_class=HTMLResponse)
    def contracts_page() -> str:
        return templates.contracts(repo.contracts_overview(pool))

    def _team_pipeline(team: str) -> str:
        return "pull-fe" if team == "frontend" else "pull-1"

    def _changes_to_work(proposals: list) -> None:
        # Group affected endpoints by team that must act (owner + consumers),
        # one goal per team with a sub-issue per endpoint, on the team's pipeline.
        by_team: dict[str, list] = {}
        for p in proposals:
            ep = f"{p['method']} {p['path']}"
            teams = {p["owner_team"]} | set(repo.consumers_of(pool, p["method"], p["path"]))
            for t in teams:
                by_team.setdefault(t, []).append((ep, p["change_type"]))
        for team, eps in by_team.items():
            pl = _team_pipeline(team)
            goal = repo.create_goal(
                pool, f"[contract] {team}: {len(eps)} contract change(s)",
                description="; ".join(f"{e} ({ct})" for e, ct in eps), pipeline=pl)
            for ep, ct in eps:
                repo.create_issue(pool, goal.id, f"{ct}: {ep}", team=team, pipeline=pl,
                                  description=f"Contract {ct} for {ep}; update {team}.")

    @app.post("/contracts/accept")
    def contract_accept(method: str = Form(...), path: str = Form(...)):
        repo.accept_proposal(pool, method, path)
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/accept_with_issue")
    def contract_accept_with_issue(method: str = Form(...), path: str = Form(...)):
        p = repo.get_proposal(pool, method, path)
        repo.accept_proposal(pool, method, path, status="agreed")
        if p:
            _changes_to_work([p])
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/accept_removal")
    def contract_accept_removal(method: str = Form(...), path: str = Form(...)):
        p = repo.get_proposal(pool, method, path)
        repo.accept_proposal(pool, method, path)  # remove -> deprecate
        if p:
            _changes_to_work([p])  # consumer cleanup
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/mark_redevelopment")
    def contract_mark_redev(method: str = Form(...), path: str = Form(...)):
        p = repo.get_proposal(pool, method, path)
        repo.reject_proposal(pool, method, path)
        if p:
            _changes_to_work([p])
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/create_work")
    def contracts_create_work():
        pending = repo.list_proposals(pool, "pending")
        _changes_to_work(pending)
        # accept add/modify shapes so consumers unblock while work proceeds;
        # leave removals for explicit Accept removal.
        for p in pending:
            if p["change_type"] in ("add", "modify"):
                repo.accept_proposal(pool, p["method"], p["path"], status="agreed")
        return RedirectResponse("/contracts", status_code=303)

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        summary = fleet_summary(pool, settings)
        summary["agents"] = agents_with_staleness(pool)
        summary["suggested_goals"] = [asdict(g) for g in
                                      repo.list_goals_by_state(pool, "suggested")]
        # jsonable_encoder handles datetimes the stdlib json encoder can't.
        return JSONResponse(jsonable_encoder(summary))

    return app
