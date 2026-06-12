"""Command-line interface for the orchestrator.

Subcommands:
  migrate                         apply pending SQL migrations
  register-agent --team --function --runtime
  add-goal "<title>" [--description ...]
  run [--max-ticks N]             drive the engine tick loop until quiescent
  serve                           start the MCP server (stdio)
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
    pool = get_pool(settings)
    goal = repo.create_goal(pool, args.title, args.description or "")
    print(f"created goal {goal.id}: {goal.title}")
    return 0


def _cmd_run(args, settings) -> int:
    from .engine.loop import Engine

    pool = get_pool(settings)
    engine = Engine(settings, pool)
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


def _cmd_serve(args, settings) -> int:
    from .mcp_server.server import main as serve_main

    serve_main()
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
    ag.set_defaults(func=_cmd_add_goal)

    rn = sub.add_parser("run", help="drive the engine until quiescent")
    rn.add_argument("--max-ticks", type=int, default=100)
    rn.set_defaults(func=_cmd_run)

    sub.add_parser("serve", help="start the MCP server (stdio)").set_defaults(func=_cmd_serve)
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
