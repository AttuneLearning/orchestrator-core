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
import os
import subprocess
import sys
from pathlib import Path

from . import repository as repo
from .config import REPO_ROOT, load_settings
from .db import close_pool, get_pool, migrate

# Issues already terminal (won't be re-cancelled when bulk-cancelling a goal).
_CANCEL_TERMINAL = {"done", "cancelled"}

# Files that affect the API contract review surface. The importer still consumes
# the explicit seed file; this list is only the tripwire that tells agents/CI
# when the seed must be staged, and when source changes likely require seed edits.
_CONTRACT_SOURCE_PREFIXES = ("packages/contracts/",)
_CONTRACT_API_PREFIX = "apps/api/src/"
_CONTRACT_API_EXACT = {"apps/api/src/app.ts"}
_CONTRACT_API_SUFFIXES = ("Router.ts",)


def _norm_repo_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _is_contract_relevant(path: str, seed_rel: str = "contracts.seed.json") -> bool:
    p = _norm_repo_rel(path)
    if p == _norm_repo_rel(seed_rel):
        return True
    if p.startswith(_CONTRACT_SOURCE_PREFIXES):
        return True
    if p in _CONTRACT_API_EXACT:
        return True
    return p.startswith(_CONTRACT_API_PREFIX) and p.endswith(_CONTRACT_API_SUFFIXES)


def _git_changed_files(repo_path: str, base_ref: str = "main") -> set[str]:
    """Return committed, staged, unstaged, and untracked paths changed in repo_path.

    `base_ref...HEAD` is the normal issue-branch view. The other diffs catch local
    edits before commit, which is how agent runs usually look immediately before
    reporting work.
    """

    def git(*args: str) -> list[str]:
        r = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return []
        return [_norm_repo_rel(line) for line in r.stdout.splitlines() if line.strip()]

    changed: set[str] = set()
    if base_ref:
        changed.update(git("diff", "--name-only", f"{base_ref}...HEAD"))
    changed.update(git("diff", "--name-only", "--cached"))
    changed.update(git("diff", "--name-only"))
    changed.update(git("ls-files", "--others", "--exclude-standard"))
    return changed


def _contract_relevant_changed_files(
    changed: set[str], seed_rel: str = "contracts.seed.json"
) -> list[str]:
    return sorted(p for p in changed if _is_contract_relevant(p, seed_rel))


def _cmd_migrate(args, settings) -> int:
    applied = migrate(settings)
    print("applied:", applied or "(up to date)")
    # Auto-bootstrap the orch-monitor KB on a fresh install (if empty).
    from .monitor_kb import bootstrap_monitor_kb
    n = bootstrap_monitor_kb(get_pool(settings), settings)
    if n:
        print(f"orch-monitor KB bootstrapped: {n} notes")
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
    maintenance = getattr(args, "maintenance", False)
    goal = repo.create_goal(
        pool, args.title, args.description or "", pipeline=args.pipeline,
        decompose=getattr(args, "decompose", None) or None,
        kind="maintenance" if maintenance else "standard",
        # A maintenance goal is a standing container, not decomposed: start it
        # 'active' so it skips the backlog→planning decomposition leg outright.
        state="active" if maintenance else "backlog")
    if maintenance:
        print(f"created maintenance goal {goal.id}: {goal.title} "
              f"(perpetual backlog; add tasks with: add-task {goal.id} \"<title>\")")
    else:
        note = f" decompose={goal.decompose}" if goal.decompose else ""
        print(f"created goal {goal.id}: {goal.title} (pipeline={goal.pipeline}{note})")
    return 0


def _cmd_add_task(args, settings) -> int:
    """Append a task to a maintenance goal's standing backlog (worked when the
    team is otherwise idle)."""
    pool = get_pool(settings)
    goal = repo.get_goal(pool, args.goal_id)
    if goal is None:
        print(f"no goal {args.goal_id}")
        return 1
    if goal.kind != "maintenance":
        print(f"goal {goal.id} is '{goal.kind}', not a maintenance goal; "
              "use add-goal --maintenance to create one")
        return 1
    pipeline = args.pipeline or goal.pipeline
    issue = repo.create_issue(pool, goal.id, args.title, args.description or "",
                              team=args.team, pipeline=pipeline)
    print(f"added maintenance task {issue.id} to goal {goal.id}: {issue.title} "
          f"(team={issue.team}, pipeline={issue.pipeline})")
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

    # No explicit pool: let create_app build the multi-coordinator registry from
    # config/instances.yaml (passing a pool forces the single-'default' test path).
    app = create_app(settings=settings)
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
    """STAGE a contract seed (a JSON array of endpoint records) as proposals diffed
    against the accepted store — add/modify/remove. Nothing becomes agreed/live until
    a human accepts it on /contracts. Treats the file as the COMPLETE endpoint set
    (absent accepted endpoints are staged as removals); --partial disables that."""
    pool = get_pool(settings)
    with open(args.path) as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        print("error: expected a JSON array of contract records", file=sys.stderr)
        return 1
    counts = repo.stage_from_seed(pool, rows, full=not args.partial)
    print(f"staged from {args.path}: {counts['add']} add, {counts['modify']} modify, "
          f"{counts['remove']} remove ({counts['skip']} unchanged). "
          "Review & accept on /contracts.")
    return 0


def _cmd_sync_contracts(args, settings) -> int:
    """Agent/CI contract tripwire.

    If contract-relevant source changed, validate the seed and stage it through
    the normal proposal workflow. With --require-seed-change, source changes
    fail unless contracts.seed.json changed too, so a new route/type cannot
    quietly skip the /contracts review page.
    """
    repo_path = str(Path(args.repo).resolve())
    seed_rel = _norm_repo_rel(args.seed)
    seed_path = Path(repo_path) / seed_rel
    changed = _git_changed_files(repo_path, args.base_ref)
    relevant = _contract_relevant_changed_files(changed, seed_rel)
    seed_changed = seed_rel in {_norm_repo_rel(p) for p in changed}

    if not relevant and not args.force:
        print("contracts sync: no contract-relevant changes")
        return 0

    if args.require_seed_change and relevant and not seed_changed and not args.force:
        print(
            "contracts sync: contract-relevant files changed but "
            f"{seed_rel} did not. Update the seed so /contracts can review the "
            "new API surface.\nchanged: " + ", ".join(relevant),
            file=sys.stderr,
        )
        return 1

    if not seed_path.is_file():
        print(f"contracts sync: missing seed file {seed_path}", file=sys.stderr)
        return 1

    try:
        with seed_path.open() as fh:
            rows = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"contracts sync: invalid JSON in {seed_path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(rows, list):
        print("contracts sync: expected a JSON array of contract records", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"contracts sync: would stage {len(rows)} contracts from {seed_path} "
            f"({len(relevant)} relevant changed file(s))"
        )
        return 0

    pool = get_pool(settings)
    counts = repo.stage_from_seed(pool, rows, full=not args.partial)
    print(
        f"contracts sync: staged from {seed_path}: {counts['add']} add, "
        f"{counts['modify']} modify, {counts['remove']} remove "
        f"({counts['skip']} unchanged). Review & accept on /contracts."
    )
    return 0


def _cmd_backup_db(args, settings) -> int:
    from .backup import backup_database

    result = backup_database(settings, reason=args.reason)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed") else 1


def _cmd_ingest_monitor_kb(args, settings) -> int:
    """Rebuild the orch-monitor knowledge base (scope monitor:kb) from current
    source — migrations, IMPLEMENTATION_SUMMARY, MCP docstrings, repo API, config,
    contract spec. Idempotent (wipe + rebuild); run after a code update."""
    from .monitor_kb import build_monitor_kb
    n = build_monitor_kb(get_pool(settings), settings)
    print(f"orch-monitor KB rebuilt: {n} notes (scope monitor:kb)")
    return 0


def _git(*a):
    import subprocess
    return subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True)


def _cmd_git_review(args, settings) -> int:
    """Read-only: check if origin is ahead; if so, alert the orch-monitor queue.
    Records last_reviewed_sha so the same commit isn't re-alerted. The actual
    update is human-gated (`self-update`)."""
    pool = get_pool(settings)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    _git("fetch", "origin", branch)
    local = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("rev-parse", f"origin/{branch}").stdout.strip()
    if not remote or local == remote:
        print(f"up to date ({branch} @ {local[:10] or '?'})")
        return 0
    if repo.get_system_state(pool, "last_reviewed_sha") == remote:
        print(f"update already alerted (origin/{branch} @ {remote[:10]})")
        return 0
    log = _git("log", "--oneline", f"{local}..{remote}").stdout.strip()
    files = _git("diff", "--name-only", local, remote).stdout.strip()
    count = len(log.splitlines())
    body = (f"origin/{branch} is {count} commit(s) ahead "
            f"({local[:10]} -> {remote[:10]}).\n\nCommits:\n{log}\n\n"
            f"Changed files:\n{files}\n\nHuman-gated update: run "
            "`python -m orchestrator.cli self-update`, then restart the daemon + "
            "dashboard to load the new code.")
    repo.create_message(pool, from_team="system", to_team="orch-monitor",
                        subject=f"Update available: {count} new commit(s) on {branch}",
                        body=body, priority="high", kind="request")
    repo.set_system_state(pool, "last_reviewed_sha", remote)
    print(f"alert posted to /orch/monitor (origin/{branch} @ {remote[:10]})")
    return 0


def _cmd_self_update(args, settings) -> int:
    """Human-gated full update: git pull -> migrate -> rebuild monitor KB. Prints
    a reminder to restart the daemon + dashboard (code reload is a manual step)."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    print(f"pulling origin/{branch} ...")
    r = _git("pull", "origin", branch)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        print("git pull failed; aborting before migrate.", file=sys.stderr)
        return 1
    print("applying migrations ...")
    print("applied:", migrate(settings) or "(up to date)")
    from .monitor_kb import build_monitor_kb
    n = build_monitor_kb(get_pool(settings), settings)
    print(f"orch-monitor KB rebuilt: {n} notes")
    print("DONE — restart the engine daemon + dashboard to load the new code.")
    return 0


def _cmd_install_launchers(args, settings) -> int:
    """Install the parent-dir agent launcher kit into a project workspace."""
    import shutil
    from pathlib import Path

    src = REPO_ROOT / "templates" / "project-launchers"
    if not src.exists():
        print(f"launcher templates missing: {src}", file=sys.stderr)
        return 1

    workspace = Path(args.workspace).expanduser().resolve()
    project = args.project or getattr(args, "instance", None) or workspace.name
    replacements = {
        "__WORKSPACE_ROOT__": str(workspace),
        "__ORCH_PATH__": str(Path(args.orchestrator_path).expanduser().resolve()),
        "__PROJECT_NAME__": project,
        "__DASHBOARD_URL__": args.dashboard_url,
    }

    planned: list[tuple[Path, Path]] = []
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        dest = workspace / rel
        if path.is_dir():
            continue
        if dest.exists() and not args.force:
            print(f"exists, not overwriting: {dest} (use --force)")
            return 1
        planned.append((path, dest))

    if args.dry_run:
        print(f"would install {len(planned)} launcher file(s) into {workspace}")
        for _, dest in planned:
            print(f"  {dest}")
        return 0

    workspace.mkdir(parents=True, exist_ok=True)
    for path, dest in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text()
        for key, val in replacements.items():
            text = text.replace(key, val)
        dest.write_text(text)
        shutil.copymode(path, dest)
        print("wrote", dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument(
        "--instance", "-i", default=None, metavar="KEY",
        help="orchestrator instance (dev group) from config/instances.yaml; "
             "selects the DB + roster + group-local settings by name instead of "
             "injecting DATABASE_URL/ROSTER_FILE. Also honored via ORCH_INSTANCE.")
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
    ag.add_argument("--maintenance", action="store_true",
                    help="create a perpetual maintenance goal (standing backlog "
                         "worked only when the team is otherwise idle)")
    ag.set_defaults(func=_cmd_add_goal)

    at = sub.add_parser("add-task", help="append a task to a maintenance goal")
    at.add_argument("goal_id", type=int)
    at.add_argument("title")
    at.add_argument("--description", default="")
    at.add_argument("--team", default="backend",
                    help="owning team (default: backend)")
    at.add_argument("--pipeline", default=None,
                    help="pipeline for this task (default: the goal's pipeline)")
    at.set_defaults(func=_cmd_add_task)

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
                        help="stage a contract seed as proposals (review/accept on /contracts)")
    ic.add_argument("path", help="path to contracts.seed.json")
    ic.add_argument("--partial", action="store_true",
                    help="seed is not the full set — don't infer removals")
    ic.set_defaults(func=_cmd_import_contracts)

    sc = sub.add_parser("sync-contracts",
                        help="agent/CI guard: detect contract changes and stage seed proposals")
    sc.add_argument("--repo", default=".",
                    help="product repository containing contracts.seed.json")
    sc.add_argument("--seed", default="contracts.seed.json",
                    help="seed path relative to --repo")
    sc.add_argument("--base-ref", default="main",
                    help="git ref for changed-file detection (default: main)")
    sc.add_argument("--partial", action="store_true",
                    help="seed is not the full set — don't infer removals")
    sc.add_argument("--force", action="store_true",
                    help="stage the seed even when no relevant changed files are detected")
    sc.add_argument("--require-seed-change", action="store_true",
                    help="fail if API/contract source changed but the seed file did not")
    sc.add_argument("--dry-run", action="store_true",
                    help="validate and report what would be staged without touching the DB")
    sc.set_defaults(func=_cmd_sync_contracts)

    bkp = sub.add_parser("backup-db",
                         help="backup the selected orchestrator coordinator database")
    bkp.add_argument("--reason", default="manual",
                     help="reason label included in the backup filename and result")
    bkp.set_defaults(func=_cmd_backup_db)

    sub.add_parser("ingest-monitor-kb",
                   help="rebuild the orch-monitor knowledge base from current source"
                   ).set_defaults(func=_cmd_ingest_monitor_kb)

    sub.add_parser("git-review",
                   help="check origin for updates; alert the orch-monitor queue if ahead"
                   ).set_defaults(func=_cmd_git_review)

    sub.add_parser("self-update",
                   help="human-gated: git pull + migrate + rebuild monitor KB"
                   ).set_defaults(func=_cmd_self_update)

    il = sub.add_parser("install-launchers",
                        help="install parent-dir agent launcher scripts into a workspace")
    il.add_argument("--workspace", required=True,
                    help="workspace parent dir where launchers should be written")
    il.add_argument("--project", default=None,
                    help="orchestrator instance/project name (default: --instance or workspace basename)")
    il.add_argument("--orchestrator-path", default=str(REPO_ROOT),
                    help="path to this orchestrator project (default: current install)")
    il.add_argument("--dashboard-url", default="http://127.0.0.1:8800",
                    help="dashboard base URL")
    il.add_argument("--force", action="store_true",
                    help="overwrite existing launcher files")
    il.add_argument("--dry-run", action="store_true",
                    help="show files that would be written")
    il.set_defaults(func=_cmd_install_launchers)

    sub.add_parser("status", help="print a state snapshot").set_defaults(func=_cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Propagate the instance so a re-load inside `serve` (mcp_server) resolves the
    # same coordinator without re-parsing argv.
    if getattr(args, "instance", None):
        os.environ["ORCH_INSTANCE"] = args.instance
    settings = load_settings(getattr(args, "instance", None))
    try:
        return args.func(args, settings)
    finally:
        if args.command != "serve":
            close_pool()


if __name__ == "__main__":
    sys.exit(main())
