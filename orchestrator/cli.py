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
  serve                           start the MCP server (stdio)
  serve-dashboard [--host --port] start the FastAPI ops dashboard
  status                          print goals / issues / agents snapshot
"""

from __future__ import annotations

import argparse
import sys

from . import repository as repo
from .config import load_settings
from .db import close_pool, get_pool, migrate


def _cmd_migrate(args, settings) -> int:
    applied = migrate(settings)
    print("applied:", applied or "(up to date)")
    return 0


def _cmd_register_agent(args, settings) -> int:
    pool = get_pool(settings)
    agent = repo.register_agent(pool, args.team, args.function, args.runtime)
    print(f"registered agent {agent.id}: {agent.team}/{agent.function} runtime={agent.runtime}")
    return 0


def _cmd_add_goal(args, settings) -> int:
    from .pipelines import load_pipelines

    known = load_pipelines(settings.pipelines)
    if args.pipeline not in known:
        print(f"unknown pipeline {args.pipeline!r}; available: {', '.join(sorted(known))}")
        return 1
    pool = get_pool(settings)
    goal = repo.create_goal(pool, args.title, args.description or "",
                            pipeline=args.pipeline)
    print(f"created goal {goal.id}: {goal.title} (pipeline={goal.pipeline})")
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


def _cmd_goal_resume(args, settings) -> int:
    pool = get_pool(settings)
    repo.resume_goal(pool, args.goal_id)
    print(f"goal {args.goal_id}: paused → active")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending SQL migrations").set_defaults(func=_cmd_migrate)

    ra = sub.add_parser("register-agent", help="register a worker agent")
    ra.add_argument("--team", required=True)
    ra.add_argument("--function", default="dev", choices=["dev", "qa"])
    ra.add_argument("--runtime", default="api", choices=["api", "cli"])
    ra.set_defaults(func=_cmd_register_agent)

    ag = sub.add_parser("add-goal", help="ingest a new goal")
    ag.add_argument("title")
    ag.add_argument("--description", default="")
    ag.add_argument("--pipeline", default="pipeline-1",
                    help="pipeline for this goal's issues (see config/pipelines.yaml)")
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

    gr = sub.add_parser("goal-resume", help="restart a paused goal")
    gr.add_argument("goal_id", type=int)
    gr.set_defaults(func=_cmd_goal_resume)

    ad = sub.add_parser("adr", help="list/show/approve ADR governance rules")
    ad.add_argument("action", choices=["list", "show", "approve"])
    ad.add_argument("key", nargs="?", default=None)
    ad.add_argument("--status", default=None,
                    help="filter list by status (proposed|accepted|...)")
    ad.set_defaults(func=_cmd_adr)

    ap = sub.add_parser("apply-promote",
                        help="merge an issue's verified worktree branch (human gate)")
    ap.add_argument("issue_id", type=int)
    ap.add_argument("--note", default="")
    ap.set_defaults(func=_cmd_apply_promote)

    sub.add_parser("serve", help="start the MCP server (stdio)").set_defaults(func=_cmd_serve)

    sd = sub.add_parser("serve-dashboard", help="start the FastAPI ops dashboard")
    sd.add_argument("--host", default="127.0.0.1")
    sd.add_argument("--port", type=int, default=8000)
    sd.set_defaults(func=_cmd_serve_dashboard)

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
