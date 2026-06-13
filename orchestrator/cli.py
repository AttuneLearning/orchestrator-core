"""Command-line interface for the orchestrator.

Subcommands:
  migrate                         apply pending SQL migrations
  register-agent --team --function --runtime
  add-goal "<title>" [--description ...]
  run [--max-ticks N] [--daemon --interval S]
                                  drive the engine tick loop
  directive <issue-id> resume [--note ...]
                                  un-quarantine an off_rails issue
  goal-resume <goal-id>           restart a paused goal
  propose-goal "<title>"          suggest a goal (gated; needs promotion)
  goal-promote <goal-id>          accept a suggested goal (suggested → backlog)
  goal-reject <goal-id>           decline a suggested goal (suggested → rejected)
  serve [--transport stdio|http]  start the MCP server (http stubbed)
  serve-dashboard [--host --port] start the FastAPI ops dashboard
  status                          print goals / issues / agents snapshot
"""

from __future__ import annotations

import argparse
import json
import sys

from . import repository as repo
from .config import load_settings
from .db import close_pool, get_pool, migrate

# Issues already terminal (won't be re-cancelled when bulk-cancelling a goal).
_CANCEL_TERMINAL = {"done", "cancelled"}


def _cmd_migrate(args, settings) -> int:
    applied = migrate(settings)
    print("applied:", applied or "(up to date)")
    return 0


def _cmd_register_agent(args, settings) -> int:
    pool = get_pool(settings)
    agent = repo.register_agent(pool, args.team, args.function, args.runtime)
    print(f"registered agent {agent.id}: {agent.team}/{agent.function} runtime={agent.runtime}")
    return 0


def _cmd_render_agent_docs(args, settings) -> int:
    import pathlib

    from . import agent_docs
    from .roster import load_roster

    pool = get_pool(settings)
    roster = load_roster(settings.roster)
    docs = agent_docs.render_for(pool, roster, args.team, args.function, args.agent_id)
    out = pathlib.Path(args.out_dir)
    if args.check:
        drifted = [f for f, content in docs.items()
                   if agent_docs.drift(content,
                                       (out / f).read_text() if (out / f).exists() else None)]
        if drifted:
            print("DRIFT: " + ", ".join(str(out / f) for f in drifted) +
                  " — out of sync with orchestrator SoT; run render-agent-docs.")
            return 1
        print("in sync with SoT:", ", ".join(docs))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for fname, content in docs.items():
        (out / fname).write_text(content)
        print("wrote", out / fname)
    return 0


def _cmd_agent_loop(args, settings) -> int:
    pool = get_pool(settings)
    enabled = True if args.enable else (False if args.disable else None)
    agent = repo.set_agent_loop(pool, args.agent, loop_enabled=enabled,
                                poll_interval_seconds=args.interval)
    if agent is None:
        print(f"no agent {args.agent}")
        return 1
    print(f"agent {agent.id} ({agent.team}/{agent.function}): "
          f"loop_enabled={agent.loop_enabled} poll_interval_seconds={agent.poll_interval_seconds}")
    return 0


def _cmd_add_goal(args, settings) -> int:
    from .pipelines import load_pipelines

    known = load_pipelines(settings.pipelines)
    if args.pipeline not in known:
        print(f"unknown pipeline {args.pipeline!r}; available: {', '.join(sorted(known))}")
        return 1
    pool = get_pool(settings)
    goal = repo.create_goal(pool, args.title, args.description or "",
                            pipeline=args.pipeline,
                            decompose=getattr(args, "decompose", None) or None)
    note = f" decompose={goal.decompose}" if goal.decompose else ""
    print(f"created goal {goal.id}: {goal.title} (pipeline={goal.pipeline}{note})")
    return 0


def _cmd_run(args, settings) -> int:
    from .engine.loop import Engine

    pool = get_pool(settings)
    engine = Engine(settings, pool)

    if args.daemon:
        def _report(summary):
            if summary.did_work or summary.errors:
                line = " ".join(f"{k}={v}" for k, v in vars(summary).items()
                                if k != "errors" and v)
                print(f"tick: {line or 'idle'}")
                for e in summary.errors:
                    print("  error:", e)
        print(f"daemon: ticking every {args.interval}s when idle (Ctrl-C to stop)")
        engine.run_daemon(interval=args.interval, on_tick=_report)
        return 0

    history = engine.run(max_ticks=args.max_ticks)
    totals = {
        "ticks": len(history),
        "decomposed": sum(s.decomposed for s in history),
        "assigned": sum(s.assigned for s in history),
        "advanced": sum(s.advanced for s in history),
        "completed": sum(s.completed for s in history),
        "failed": sum(s.failed for s in history),
        "quarantined": sum(s.quarantined for s in history),
        "reengaged": sum(s.reengaged for s in history),
        "goals_done": sum(s.goals_done for s in history),
        "goals_paused": sum(s.goals_paused for s in history),
    }
    errors = [e for s in history for e in s.errors]
    print("run summary:", totals)
    if errors:
        print("errors:")
        for e in errors:
            print("  -", e)
    return 0


def _cmd_directive(args, settings) -> int:
    pool = get_pool(settings)
    issue = repo.apply_directive(pool, args.issue_id, args.action, note=args.note)
    print(f"issue {issue.id}: directive '{args.action}' applied → {issue.state} "
          f"(gate={issue.gate_type}, retries/steps reset)")
    return 0


def _cmd_cancel(args, settings) -> int:
    """Bulk-cancel issues for triage: by id, all failed, all off_rails, or all
    open issues of a goal. Cancelled issues are terminal and release their agent."""
    pool = get_pool(settings)
    targets: list = []
    if args.issue_id is not None:
        targets = [args.issue_id]
    elif args.goal is not None:
        targets = [i.id for i in repo.list_issues(pool, goal_id=args.goal)
                   if i.state not in _CANCEL_TERMINAL]
    else:
        states = []
        if args.failed:
            states.append("failed")
        if args.off_rails:
            states.append("off_rails")
        if not states:
            print("specify --issue-id, --goal, --failed, and/or --off-rails")
            return 1
        targets = [i.id for i in repo.list_issues(pool, states=states)]
    if not targets:
        print("no matching issues to cancel")
        return 0
    n = 0
    for iid in targets:
        try:
            repo.cancel_issue(pool, iid, reason=args.reason, actor="operator")
            n += 1
        except ValueError as exc:
            print(f"  skip issue {iid}: {exc}")
    print(f"cancelled {n} issue(s)" + (f" — {args.reason!r}" if args.reason else ""))
    return 0


def _cmd_goal_resume(args, settings) -> int:
    pool = get_pool(settings)
    repo.resume_goal(pool, args.goal_id)
    print(f"goal {args.goal_id}: paused → active")
    return 0


def _cmd_propose_goal(args, settings) -> int:
    from .pipelines import load_pipelines

    known = load_pipelines(settings.pipelines)
    if args.pipeline not in known:
        print(f"unknown pipeline {args.pipeline!r}; available: {', '.join(sorted(known))}")
        return 1
    pool = get_pool(settings)
    goal = repo.propose_goal(pool, args.title, args.description or "",
                             pipeline=args.pipeline, suggested_by=args.suggested_by,
                             source=args.source)
    print(f"suggested goal {goal.id}: {goal.title} — promote to queue it for work")
    return 0


def _cmd_goal_promote(args, settings) -> int:
    pool = get_pool(settings)
    repo.promote_goal(pool, args.goal_id)
    print(f"goal {args.goal_id}: suggested → backlog (engine will pick it up)")
    return 0


def _cmd_goal_reject(args, settings) -> int:
    pool = get_pool(settings)
    repo.reject_goal(pool, args.goal_id)
    print(f"goal {args.goal_id}: suggested → rejected")
    return 0


def _cmd_adr(args, settings) -> int:
    pool = get_pool(settings)
    if args.action == "list":
        for a in repo.list_adrs(pool, status=args.status):
            sel = a["applies_to"] or {}
            scope = ",".join(sel.get("repos") or []) or "project-wide"
            print(f"  [{a['adr_key']}] {a['status']:10} ({scope}) {a['title']}")
        return 0
    if args.key is None:
        print("adr show/approve require an ADR key")
        return 1
    if args.action == "show":
        a = repo.get_adr(pool, args.key)
        if a is None:
            print(f"no ADR {args.key}")
            return 1
        print(f"{a['adr_key']}: {a['title']} [{a['status']}] (by {a['proposed_by']})")
        print(f"  rule:       {a['decision']}")
        print(f"  rationale:  {a['context']}")
        print(f"  applies_to: {a['applies_to']}")
        print(f"  related: {a['related']}  supersedes: {a['supersedes']}  "
              f"patterns: {a['patterns']}")
        return 0
    if args.action == "approve":
        a = repo.approve_adr(pool, args.key)
        print(f"{a['adr_key']}: proposed → accepted (live for agents next tick)")
        return 0
    return 1


def _cmd_apply_promote(args, settings) -> int:
    from .apply.worktree import promote

    pool = get_pool(settings)
    issue = repo.get_issue(pool, args.issue_id)
    if issue is None:
        print(f"no issue {args.issue_id}")
        return 1
    record = promote(pool, issue, settings, note=args.note)
    print(f"issue {issue.id}: branch {record['branch']} merged "
          f"({record['merge_commit'][:10]}) — local only, nothing pushed")
    return 0


def _cmd_serve(args, settings) -> int:
    if args.transport == "http":
        # Scaffold only: external looping agents that can't co-locate will use
        # this. The plumbing (host/port/token) is parsed here; wiring FastMCP's
        # streamable-http transport + a bearer-token check is the remaining work.
        # See docs/PLUGIN_INTEGRATION.md ("Remote / HTTP — coming soon").
        import os

        token = os.environ.get("ORCH_MCP_TOKEN", "")
        # TODO: build_server().run(transport="streamable-http", host=args.host,
        #       port=args.port) behind bearer-token middleware validating `token`.
        print(
            "serve --transport http is not implemented yet (stub).\n"
            f"  would bind http://{args.host}:{args.port}; "
            f"token {'set' if token else 'NOT set (export ORCH_MCP_TOKEN)'}.\n"
            "  Use --transport stdio (default) for now; see "
            "docs/PLUGIN_INTEGRATION.md."
        )
        return 2

    from .mcp_server.server import main as serve_main

    serve_main()
    return 0


def _cmd_serve_dashboard(args, settings) -> int:
    import uvicorn

    from .dashboard.app import create_app

    app = create_app(get_pool(settings), settings)
    print(f"dashboard: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_status(args, settings) -> int:
    pool = get_pool(settings)
    print("== goals ==")
    for g in repo.list_open_goals(pool):
        print(f"  [{g.id}] {g.state:8} {g.title}")
    print("== issues ==")
    for i in repo.list_issues(pool):
        gate = f" gate={i.gate_type}" if i.gate_type else ""
        print(f"  [{i.id}] goal={i.goal_id} {i.state:11} retry={i.retry_count} "
              f"step={i.step_count}{gate}  {i.title}")
    print("== agents ==")
    for a in repo.list_agents(pool):
        print(f"  [{a.id}] {a.team}/{a.function} {a.status} runtime={a.runtime}")
    pending = repo.pending_messages(pool)
    if pending:
        print("== pending messages ==")
        for m in pending:
            print(f"  [{m['id']}] {m['from_team']} → {m['to_team']} "
                  f"({m['priority']}): {m['subject']}")
    return 0


def _cmd_import_contracts(args, settings) -> int:
    """Seed/refresh the contract store from a JSON array of endpoint records
    (the API repo's contracts/seed/contracts.seed.json). Idempotent on method+path."""
    pool = get_pool(settings)
    with open(args.path) as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        print("error: expected a JSON array of contract records", file=sys.stderr)
        return 1
    n = 0
    for r in rows:
        repo.upsert_contract(
            pool,
            method=r["method"],
            path=r["path"],
            request_ref=r.get("request_ref", ""),
            response_dto=r.get("response_dto", ""),
            auth=r.get("auth", "none"),
            owner_team=r.get("owner_team", "backend"),
            status=r.get("status", "proposed"),
            version=str(r.get("version", "1.0")),
            source_ref=r.get("source_ref"),
        )
        n += 1
    print(f"imported {n} contracts from {args.path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending SQL migrations").set_defaults(func=_cmd_migrate)

    ra = sub.add_parser("register-agent", help="register a worker agent")
    ra.add_argument("--team", required=True)
    ra.add_argument("--function", default="dev", choices=["dev", "qa", "lead"])
    ra.add_argument("--runtime", default="api", choices=["api", "cli", "external"])
    ra.set_defaults(func=_cmd_register_agent)

    ag = sub.add_parser("add-goal", help="ingest a new goal")
    ag.add_argument("title")
    ag.add_argument("--description", default="")
    ag.add_argument("--pipeline", default="pipeline-1",
                    help="pipeline for this goal's issues (see config/pipelines.yaml)")
    ag.add_argument("--decompose", choices=["single", "full"], default=None,
                    help="override the heuristic: 'single' = one impl issue, "
                         "'full' = force decomposition (default: auto)")
    ag.set_defaults(func=_cmd_add_goal)

    rn = sub.add_parser("run", help="drive the engine until quiescent")
    rn.add_argument("--max-ticks", type=int, default=100)
    rn.add_argument("--daemon", action="store_true",
                    help="tick forever instead of stopping when quiescent")
    rn.add_argument("--interval", type=float, default=5.0,
                    help="seconds between ticks when idle (daemon mode)")
    rn.set_defaults(func=_cmd_run)

    dr = sub.add_parser("directive", help="un-quarantine an off_rails issue")
    dr.add_argument("issue_id", type=int)
    dr.add_argument("action", choices=["resume"])
    dr.add_argument("--note", default="")
    dr.set_defaults(func=_cmd_directive)

    cx = sub.add_parser("cancel", help="bulk-cancel issues for triage (terminal)")
    cx.add_argument("--issue-id", type=int, default=None, help="cancel one issue")
    cx.add_argument("--goal", type=int, default=None,
                    help="cancel all open issues of a goal")
    cx.add_argument("--failed", action="store_true", help="cancel all failed issues")
    cx.add_argument("--off-rails", dest="off_rails", action="store_true",
                    help="cancel all off_rails issues")
    cx.add_argument("--reason", default="", help="reason recorded on the cancel event")
    cx.set_defaults(func=_cmd_cancel)

    gr = sub.add_parser("goal-resume", help="restart a paused goal")
    gr.add_argument("goal_id", type=int)
    gr.set_defaults(func=_cmd_goal_resume)

    pg = sub.add_parser("propose-goal",
                        help="suggest a goal (gated; needs promotion to run)")
    pg.add_argument("title")
    pg.add_argument("--description", default="")
    pg.add_argument("--pipeline", default="pipeline-1")
    pg.add_argument("--suggested-by", dest="suggested_by", default="human")
    pg.add_argument("--source", default="")
    pg.set_defaults(func=_cmd_propose_goal)

    gp = sub.add_parser("goal-promote",
                        help="accept a suggested goal into the queue (suggested → backlog)")
    gp.add_argument("goal_id", type=int)
    gp.set_defaults(func=_cmd_goal_promote)

    gj = sub.add_parser("goal-reject", help="decline a suggested goal (suggested → rejected)")
    gj.add_argument("goal_id", type=int)
    gj.set_defaults(func=_cmd_goal_reject)

    ad = sub.add_parser("adr", help="list/show/approve ADR governance rules")
    ad.add_argument("action", choices=["list", "show", "approve"])
    ad.add_argument("key", nargs="?", default=None)
    ad.add_argument("--status", default=None,
                    help="filter list by status (proposed|accepted|...)")
    ad.set_defaults(func=_cmd_adr)

    rd = sub.add_parser("render-agent-docs",
                        help="generate/check CLAUDE.md+AGENTS.md from the orchestrator SoT")
    rd.add_argument("--team", required=True)
    rd.add_argument("--function", default="dev", choices=["dev", "qa", "lead"])
    rd.add_argument("--agent-id", type=int, required=True)
    rd.add_argument("--out-dir", required=True, help="repo root to write/check the files")
    rd.add_argument("--check", action="store_true",
                    help="diff against committed files; exit 1 on drift (CI/pre-engage)")
    rd.set_defaults(func=_cmd_render_agent_docs)

    al = sub.add_parser("agent-loop", help="set a pull worker's loop policy (cadence)")
    al.add_argument("--agent", type=int, required=True)
    grp = al.add_mutually_exclusive_group()
    grp.add_argument("--enable", action="store_true", help="keep looping when idle")
    grp.add_argument("--disable", action="store_true", help="stop after the queue drains")
    al.add_argument("--interval", type=int, default=None,
                    help="idle poll seconds when enabled (60-7200)")
    al.set_defaults(func=_cmd_agent_loop)

    ap = sub.add_parser("apply-promote",
                        help="merge an issue's verified worktree branch (human gate)")
    ap.add_argument("issue_id", type=int)
    ap.add_argument("--note", default="")
    ap.set_defaults(func=_cmd_apply_promote)

    sv = sub.add_parser("serve", help="start the MCP server (stdio; http stubbed)")
    sv.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                    help="stdio (default) or http (not yet implemented; reads "
                         "ORCH_MCP_TOKEN for future auth)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=_cmd_serve)

    sd = sub.add_parser("serve-dashboard", help="start the FastAPI ops dashboard")
    sd.add_argument("--host", default="127.0.0.1")
    sd.add_argument("--port", type=int, default=8000)
    sd.set_defaults(func=_cmd_serve_dashboard)

    ic = sub.add_parser("import-contracts",
                        help="seed the contract store from a JSON array (method+path keyed)")
    ic.add_argument("path", help="path to contracts.seed.json")
    ic.set_defaults(func=_cmd_import_contracts)

    sub.add_parser("status", help="print a state snapshot").set_defaults(func=_cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    try:
        return args.func(args, settings)
    finally:
        if args.command != "serve":
            close_pool()


if __name__ == "__main__":
    sys.exit(main())
