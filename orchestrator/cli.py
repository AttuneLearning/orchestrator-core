"""Command-line interface for the orchestrator.

Subcommands:
  migrate                         apply pending SQL migrations
  register-agent --team --function --runtime
  add-goal "<title>" [--description ...]
  install-launchers --workspace PATH
                                  install the parent-dir launcher kit
  setup-project --workspace PATH   scaffold a project workspace and launchers
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

from . import decomposition, repository as repo
from .config import REPO_ROOT, load_settings
from .db import close_pool, get_pool, migrate
from .roster import load_roster

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


def _launcher_template_root(orchestrator_path: str | None = None) -> Path:
    base = Path(orchestrator_path).expanduser().resolve() if orchestrator_path else REPO_ROOT
    return base / "templates" / "project-launchers"


# Per-project files install-launchers must never overwrite (only seed when
# absent): each workspace customizes these (dashboard URL, tier, secrets,
# YOLO flags). A --force push that clobbered them would destroy per-project
# config — the env-clobber gap closed by the durable-worker plan (§8).
_PRESERVE_IF_PRESENT = ("orchestrator.env", "secrets.env")
_PRESERVE_SUFFIXES = ("-yolo.env",)


def _is_preserved(rel: "Path") -> bool:
    return rel.name in _PRESERVE_IF_PRESENT or rel.name.endswith(_PRESERVE_SUFFIXES)


def _launcher_copy_plan(
    src: Path,
    workspace: Path,
    replacements: dict[str, str],
    *,
    force: bool,
) -> list[tuple[Path, Path]]:
    if not src.exists():
        raise FileNotFoundError(f"launcher templates missing: {src}")

    planned: list[tuple[Path, Path]] = []
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or rel.suffix == ".pyc":
            continue
        dest = workspace / rel
        if dest.exists() and _is_preserved(rel):
            continue  # per-project file: seed only when absent, never overwrite
        if dest.exists() and not force:
            raise FileExistsError(f"exists, not overwriting: {dest} (use --force)")
        planned.append((path, dest))
    return planned


def _write_launcher_plan(
    planned: list[tuple[Path, Path]],
    replacements: dict[str, str],
) -> None:
    for path, dest in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text()
        for key, val in replacements.items():
            text = text.replace(key, val)
        # Atomic replace: running shells keep the old inode; readers never see
        # a truncated file mid-write.
        tmp = dest.parent / (dest.name + ".tmp-install")
        tmp.write_text(text)
        subprocess.run(["chmod", "+x", str(tmp)], check=False)
        os.replace(tmp, dest)
        print("wrote", dest)


def _workspace_launch_plan(
    settings,
    workspace: Path,
    worktree_prefix: str = "wt-",
    humantest_worktree: str = "humantest-wt",
) -> list[Path]:
    roster = load_roster(settings.roster)
    planned: list[Path] = [workspace / humantest_worktree]
    seen: set[str] = set()
    for team in roster.teams.values():
        for sub in team.sub_teams:
            if sub.mode != "pull":
                continue
            worktree_name = f"{worktree_prefix}{sub.id}"
            if worktree_name in seen:
                continue
            seen.add(worktree_name)
            planned.append(workspace / worktree_name)
    return planned


def _print_setup_plan(
    workspace: Path,
    worktree_dirs: list[Path],
    launcher_files: list[tuple[Path, Path]],
) -> None:
    print(f"workspace={workspace}")
    for path in worktree_dirs:
        print(f"worktree={path}")
    print(f"launcher_files={len(launcher_files)}")
    for _, dest in launcher_files:
        print(f"launcher={dest}")


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
    tier = decomposition.resolve_tier(settings.decomposition_tier)
    docs = agent_docs.render_for(pool, roster, args.team, args.function, args.agent_id,
                                 internal_parallelism=tier.internal_parallelism,
                                 midrun_checks=tier.midrun_checks)
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
    host = args.host if args.host is not None else settings.dashboard_host
    port = args.port if args.port is not None else settings.dashboard_port
    app = create_app(settings=settings)
    print(f"dashboard: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
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
    tiers = repo.worker_tier_stats(pool)
    if tiers:
        print("== worker tiers (GAP-5 stamps; see /tiers) ==")
        for t in tiers:
            print(f"  {t['team']}/{t['runtime']}: commits={t['commits']} "
                  f"verify={t['verify_green']}/{t['verifies']} "
                  f"(avg {t['avg_verify_s'] or '—'}s) "
                  f"gates +{t['gate_pass']}/-{t['gate_decline']}")
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


def _cmd_contracts_lifecycle_apply(args, settings) -> int:
    """Apply a contract lifecycle batch through the single audited/idempotent
    repository path (never direct SQL). --file is a JSON array of change objects
    ({contract_id, action, replacement_contract_id?, source_ref?})."""
    pool = get_pool(settings)
    with open(args.file) as fh:
        changes = json.load(fh)
    if not isinstance(changes, list):
        print("error: --file must be a JSON array of change objects", file=sys.stderr)
        return 1
    result = repo.contract_lifecycle_apply(
        pool, project=args.project, operation_id=args.op,
        actor="cli-admin", actor_role="orch-manager", reason=args.reason,
        changes=changes, source="cli",
        confirm_project=args.confirm_project or None)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("result") == "applied" else 1


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


def _cmd_contracts_drift(args, settings) -> int:
    """Read-only contract drift report for the active instance. Cross-references
    the registry against contracts.audit.json and the frontend endpoint registry /
    MSW mocks, printing a BLOCKING/ADVISORY report. Exits non-zero when blocking
    findings exist and --strict is set."""
    from . import contract_drift

    pool = get_pool(settings)
    try:
        report = contract_drift.run_drift_check(pool, settings)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"drift check failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        # Human-readable report: header, BLOCKING section, ADVISORY section,
        # then by-category summary.
        findings = report["findings"]
        summary = report["summary"]
        audit_path = report.get("audit_path", "")
        fe_root = report.get("fe_root", "")

        # Header
        print(f"Contract Drift Report")
        if audit_path:
            print(f"  audit: {audit_path}")
        if fe_root:
            print(f"  fe_root: {fe_root}")
        print(f"  blocking: {summary['blocking']}, advisory: {summary['advisory']}, "
              f"total: {summary['total']}")
        print()

        # Filter by severity
        severity_filter = args.severity
        blocking_findings = [f for f in findings if f["severity"] == "blocking"]
        advisory_findings = [f for f in findings if f["severity"] == "advisory"]

        # BLOCKING section
        if severity_filter in ("blocking", "all"):
            if blocking_findings:
                print(f"BLOCKING ({len(blocking_findings)}):")
                for finding in blocking_findings:
                    contract_ref = f"(#{finding['contract_id']})" if finding["contract_id"] else "(—)"
                    method = finding["method"] or "—"
                    print(f"  [{finding['category']}] {method:6} {finding['path']:40} "
                          f"{contract_ref:15} — {finding['detail']}")
            else:
                print("BLOCKING: None")
            print()

        # ADVISORY section
        if severity_filter in ("advisory", "all"):
            if advisory_findings:
                print(f"ADVISORY ({len(advisory_findings)}):")
                for finding in advisory_findings:
                    contract_ref = f"(#{finding['contract_id']})" if finding["contract_id"] else "(—)"
                    method = finding["method"] or "—"
                    print(f"  [{finding['category']}] {method:6} {finding['path']:40} "
                          f"{contract_ref:15} — {finding['detail']}")
            else:
                print("ADVISORY: None")
            print()

        # By-category summary
        if summary.get("by_category"):
            print("By category:")
            for cat, count in sorted(summary["by_category"].items()):
                print(f"  {cat}: {count}")

    blocking = report["summary"]["blocking"]
    return 1 if (args.strict and blocking) else 0


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
    workspace = Path(args.workspace).expanduser().resolve()
    project = args.project or getattr(args, "instance", None) or workspace.name
    replacements = {
        "__WORKSPACE_ROOT__": str(workspace),
        "__ORCH_PATH__": str(Path(args.orchestrator_path).expanduser().resolve()),
        "__PROJECT_NAME__": project,
        "__DASHBOARD_URL__": args.dashboard_url,
    }
    src = _launcher_template_root(args.orchestrator_path)
    try:
        planned = _launcher_copy_plan(src, workspace, replacements, force=args.force)
    except (FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"would install {len(planned)} launcher file(s) into {workspace}")
        for _, dest in planned:
            print(f"  {dest}")
        return 0

    workspace.mkdir(parents=True, exist_ok=True)
    _write_launcher_plan(planned, replacements)
    return 0


def _cmd_setup_project(args, settings) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    project = args.project or getattr(args, "instance", None) or workspace.name
    worktree_prefix = getattr(args, "worktree_prefix", "wt-")
    humantest_worktree = getattr(args, "humantest_worktree", "humantest-wt")
    # Bootstrap decomposition tier: scales granularity/parallelism/drift-checks to
    # the fleet's models. Normalized through resolve_tier so an unknown value can't
    # scaffold a broken project.
    tier = decomposition.resolve_tier(
        getattr(args, "decomposition_tier", None) or decomposition.DEFAULT_TIER)
    replacements = {
        "__WORKSPACE_ROOT__": str(workspace),
        "__ORCH_PATH__": str(Path(args.orchestrator_path).expanduser().resolve()),
        "__PROJECT_NAME__": project,
        "__DASHBOARD_URL__": args.dashboard_url,
        "__DECOMPOSITION_TIER__": tier.name,
    }
    src = _launcher_template_root(args.orchestrator_path)
    try:
        planned_launchers = _launcher_copy_plan(src, workspace, replacements, force=args.force)
    except (FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    planned_worktrees = _workspace_launch_plan(settings, workspace, worktree_prefix, humantest_worktree)

    if args.dry_run:
        print(f"would create workspace: {workspace}")
        for path in planned_worktrees:
            print(f"would create worktree: {path}")
        print(f"would install {len(planned_launchers)} launcher file(s)")
        for _, dest in planned_launchers:
            print(f"  {dest}")
        _print_tier_guidance(tier, project)
        return 0

    workspace.mkdir(parents=True, exist_ok=True)
    for path in planned_worktrees:
        path.mkdir(parents=True, exist_ok=True)
        print("created", path)
    _write_launcher_plan(planned_launchers, replacements)
    _print_tier_guidance(tier, project)
    _offer_watchdog(workspace)
    return 0


def _print_tier_guidance(tier, project: str) -> None:
    """Report the chosen decomposition tier and print the authoritative config line
    to pin it per-project. The launcher env carries DECOMPOSITION_TIER for the
    scaffolded workspace; adding it to the instance's `settings:` block makes it the
    coordinator's source of truth (config precedence: env > this yaml > tier default)."""
    print(f"\ndecomposition tier: {tier.name}"
          f"  (internal_parallelism={tier.internal_parallelism}, "
          f"midrun_checks={tier.midrun_checks})")
    print("To make it authoritative for the coordinator, add to the instance's "
          "settings block in config/instances.yaml:")
    print(f"  instances:\n    {project}:\n      settings:\n"
          f"        decomposition_tier: {tier.name}")


def _offer_watchdog(workspace: Path) -> None:
    """Opt-in: the worker watchdog auto-restarts a stalled worker ONCE (only when its
    heartbeat has stopped and work is waiting). Because it kills/relaunches processes
    it is never enabled silently — we ASK when interactive, and print the manual
    command otherwise. install-watchdog.sh itself also prompts before installing."""
    installer = workspace / "install-watchdog.sh"
    if not installer.exists():
        return
    manual = f"{installer}          # enable the worker-watchdog cron (asks first)"
    if not sys.stdin.isatty():
        print(f"\nOptional: a worker-watchdog cron can auto-restart a stalled worker once.\n"
              f"Enable it when you want with:\n  {manual}")
        return
    try:
        ans = input("\nEnable the worker-watchdog cron now? It hard-restarts a stalled worker "
                    "ONCE when work is waiting (opt-in) [y/N]: ").strip().lower()
    except EOFError:
        ans = ""
    if ans in ("y", "yes"):
        subprocess.run([str(installer), "--install", "--yes"], cwd=str(workspace))
    else:
        print(f"Skipped. Enable later with:\n  {manual}")


def _ensure_database(database_url: str) -> bool:
    """Create the target Postgres database if it doesn't exist. Returns True if it
    was created. Connects to the server's default 'postgres' maintenance DB (CREATE
    DATABASE cannot run inside a transaction, so autocommit)."""
    import psycopg
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(database_url)
    dbname = parts.path.lstrip("/")
    if not dbname:
        raise ValueError(f"database_url has no database name: {database_url!r}")
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if exists:
            return False
        conn.execute(f'CREATE DATABASE "{dbname}"')
        return True


def _run_doctor_checks(settings) -> list:
    """Run the preflight checks against a resolved instance. Pure-ish: each check is
    isolated so one failure (e.g. DB down) still reports the rest."""
    from . import db, onboarding
    from .pipelines import load_pipelines
    from .roster import load_roster
    C, P, W, F = onboarding.Check, onboarding.PASS, onboarding.WARN, onboarding.FAIL
    checks: list = [C("config", P,
                      f"tier={settings.decomposition_tier}, "
                      f"default_pipeline={settings.default_pipeline}")]

    default_team = None
    try:
        pipelines = load_pipelines(settings.pipelines)
        if settings.default_pipeline in pipelines:
            checks.append(C("pipelines", P, f"{len(pipelines)} defined; default OK"))
            default_team = getattr(pipelines[settings.default_pipeline], "team", None)
        else:
            checks.append(C("pipelines", F,
                            f"default_pipeline {settings.default_pipeline!r} not in "
                            f"{sorted(pipelines)}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(C("pipelines", F, str(exc)))

    try:
        roster = load_roster(settings.roster)
        if default_team and roster.resolve(default_team) is None:
            checks.append(C("roster", F, f"pipeline team {default_team!r} not in roster"))
        else:
            checks.append(C("roster", P, f"resolves ({settings.roster_file})"))
    except Exception as exc:  # noqa: BLE001
        checks.append(C("roster", F, str(exc)))

    try:
        pool = db.get_pool(settings)
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        checks.append(C("database", P, "reachable"))
        files = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
        with pool.connection() as conn:
            try:
                done = {r[0] for r in conn.execute(
                    "SELECT filename FROM schema_migrations").fetchall()}
            except Exception:  # noqa: BLE001 - table missing = nothing applied
                done = set()
        pending = [f for f in files if f not in done]
        if not done:
            checks.append(C("migrations", F, "none applied — run `migrate`"))
        elif pending:
            checks.append(C("migrations", F, f"{len(pending)} pending — run `migrate`"))
        else:
            checks.append(C("migrations", P, f"{len(files)} applied"))
        agents = repo.list_agents(pool)
        checks.append(C("agents", P if agents else W,
                        f"{len(agents)} registered" if agents
                        else "none — `register-agent` or `init`"))
        goals = repo.list_open_goals(pool)
        checks.append(C("goals", P if goals else W,
                        f"{len(goals)} open" if goals
                        else "none — `add-goal` to start work"))
    except Exception as exc:  # noqa: BLE001
        checks.append(C("database", F, f"unreachable: {exc}"))

    backend = settings.reasoner or ("anthropic" if settings.anthropic_api_key else "")
    if backend and backend != "stub":
        checks.append(C("reasoner", onboarding.PASS,
                        f"backend={backend}, "
                        f"model={settings.reasoner_model or settings.reasoning_model}"))
    else:
        checks.append(C("reasoner", W,
                        "stub/unset — engine decisions will be deterministic placeholders"))

    # --- Workflow profile lints (WP-17) ---
    # Lint 4: workflow_profile enabled but workspace_manifest unset or missing
    if settings.workflow_profile == "enabled":
        manifest_str = str(settings.workspace_manifest or "").strip()
        if not manifest_str:
            checks.append(C("workflow_profile", F,
                            "workflow_profile=enabled but workspace_manifest is unset"))
        else:
            manifest_path = Path(manifest_str)
            if not manifest_path.is_file():
                checks.append(C("workflow_profile", F,
                                f"workspace_manifest file not found: {manifest_str}"))

    # Worktree-dependent lints: get first verify_worktrees value if available
    worktree = None
    if settings.verify_worktrees:
        worktree = next(iter(settings.verify_worktrees.values()), None)

    if worktree:
        try:
            from .workflow import loader, models, permissions as perm_module, adapters

            # Load the effective profile
            profile = loader.load_effective(settings, worktree)

            # Lint 1: Profile has warnings
            if profile.warnings:
                for warning in profile.warnings:
                    checks.append(C("workflow_profile", W, f"profile warning: {warning}"))

            # Lint 2: Validate all actions in the profile
            validation_problems = models.validate(profile)
            for problem in validation_problems:
                checks.append(C("workflow_profile", W, f"validation: {problem}"))

            # Lint 3: Actions with escalate verdict
            perms = loader.load_permissions(settings)
            escalate_actions = []
            for step in profile.steps.values():
                # Check role-agnostic actions
                for action in step.actions:
                    if perm_module.authorize(action, perms) == "escalate":
                        escalate_actions.append(
                            f"{step.name}: {action.run or action.builtin}"
                        )
                # Check role-specific actions
                for role, actions in step.by_role.items():
                    for action in actions:
                        if perm_module.authorize(action, perms) == "escalate":
                            escalate_actions.append(
                                f"{step.name}[{role}]: {action.run or action.builtin}"
                            )
            if escalate_actions:
                checks.append(C("workflow_profile", W,
                                f"{len(escalate_actions)} custom action(s) will require approval: "
                                + "; ".join(escalate_actions)))

            # Lint 5: Repo profile has permissions key (detected via loader warning)
            # The loader produces a warning when it detects repo-layer permissions.
            # Check if any warning mentions "permissions".
            if any("permissions" in w for w in profile.warnings):
                checks.append(C("workflow_profile", F,
                                "repo profile contains permissions: key (self-authorization "
                                "not allowed — remove it and set permissions in workspace manifest)"))

            # Lint 6: Stack has no adapter or empty default steps
            if profile.stack:
                adapter = adapters.get_adapter(profile.stack)
                if adapter is None:
                    checks.append(C("workflow_profile", W,
                                    f"detected stack '{profile.stack}' has no adapter"))
                else:
                    steps = adapter.default_steps()
                    if not steps:
                        checks.append(C("workflow_profile", W,
                                        f"detected stack '{profile.stack}' adapter has no default steps"))

        except Exception as exc:  # noqa: BLE001
            # Profile loading or lint execution failed — report as warning, not blocker
            checks.append(C("workflow_profile", W,
                            f"could not load effective profile: {exc}"))

    return checks


def _cmd_doctor(args, settings) -> int:
    from . import onboarding
    checks = _run_doctor_checks(settings)
    code, report = onboarding.summarize(checks)
    print(f"orchestrator doctor ({settings.roster_file}):")
    print(report)
    return code


def _cmd_workflow_explain(args, settings) -> int:
    """Print each action in the effective workflow profile with its authorization verdict.

    Loads the profile from the three layers (defaults, repo, workspace) and authorizes
    each action against the workspace-granted permissions. Outputs one line per action
    with step, role-scope, verdict, source, and the run/builtin string.

    Exit codes: 0 normally; 2 if any action verdict is 'deny' OR the profile has warnings.
    """
    from pathlib import Path
    from .workflow import loader, permissions as perm_module

    # Resolve the team's worktree
    team = getattr(args, "team", None)
    if team and team in settings.verify_worktrees:
        worktree = Path(settings.verify_worktrees[team])
    else:
        worktree = Path.cwd()

    # Role defaults to "qa"
    role = getattr(args, "role", None) or "qa"

    # Load the effective profile and permissions
    try:
        profile = loader.load_effective(settings, worktree, role=role)
        perms = loader.load_permissions(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to load workflow profile: {exc}", file=sys.stderr)
        return 2

    # Collect any violations
    has_deny = False
    has_warnings = bool(profile.warnings)

    # Print each step's actions
    for step_name in profile.steps:
        step = profile.step(step_name)
        actions = step.actions_for(role)
        for action in actions:
            verdict = perm_module.authorize(action, perms)
            if verdict == "deny":
                has_deny = True

            # Format role-scope: show it only if there's a role-specific action
            role_scope = f" [{role}]" if role and role in step.by_role else ""

            # Format source
            source_str = f" [{action.source}]" if action.source else ""

            # Format the run or builtin string
            run_or_builtin = action.run if action.run else f"@{action.builtin}"

            print(f"{step_name}{role_scope} {verdict}{source_str} {run_or_builtin}")

    # Print warnings
    for warning in profile.warnings:
        print(f"warning: {warning}")

    # Exit code: 0 normally, 2 if any deny or warnings
    if has_deny or has_warnings:
        return 2
    return 0


def _cmd_init(args, settings) -> int:
    """Guided project bootstrap: pick a tier, write a drop-in instance config, create
    + migrate the DB, register agents, then run the preflight (doctor)."""
    from . import db, onboarding

    project = args.project
    label = args.label or project.replace("-", " ").replace("_", " ").title()
    roster_file = args.roster_file
    database_url = args.database_url or \
        f"postgresql://orchestrator:orchestrator@localhost:5432/{project}"
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()]
    if args.decomposition_tier:
        tier = decomposition.resolve_tier(args.decomposition_tier)
        tier_reason = "explicit --decomposition-tier"
    else:
        tier = decomposition.resolve_tier(onboarding.recommend_tier(models))
        tier_reason = f"recommended from models {models}" if models \
            else "default (pass --models or --decomposition-tier to tune)"
    teams = [t.strip() for t in (args.teams or "backend,frontend").split(",") if t.strip()]
    agent_plan = [(t, f) for t in teams for f in ("dev", "qa")]

    dropin_path = REPO_ROOT / "config" / "instances.d" / f"{project}.yaml"
    entry = onboarding.build_instance_entry(
        label=label, database_url=database_url, roster_file=roster_file, tier=tier.name)
    body = onboarding.render_instances_dropin(project, entry)

    print(f"init plan for project {project!r}:")
    print(f"  decomposition tier : {tier.name}  ({tier_reason})")
    print(f"  database_url       : {database_url}")
    print(f"  roster_file        : {roster_file}")
    print(f"  instance config    : {dropin_path}")
    print(f"  agents to register : " + ", ".join(f"{t}/{f}" for t, f in agent_plan))

    if args.dry_run:
        print(f"\n(dry-run) no changes made.\n--- would write {dropin_path} ---\n{body}")
        return 0
    if not args.yes and sys.stdin.isatty():
        try:
            if input("\nProceed? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("aborted.")
                return 1
        except EOFError:
            print("aborted (no tty; pass --yes to run non-interactively).")
            return 1

    dropin_path.parent.mkdir(parents=True, exist_ok=True)
    if dropin_path.exists() and not args.force:
        print(f"refusing to overwrite {dropin_path} (pass --force)", file=sys.stderr)
        return 1
    dropin_path.write_text(body)
    print("wrote", dropin_path)

    # Scaffold the project's roster if it doesn't exist yet. Real project
    # rosters (config/roster.<project>.yaml) are gitignored — the tool ships
    # only templates — so the install wizard must create a working one from
    # config/roster.example.pull.yaml. The default --roster-file points at the
    # tracked generic roster.yaml, which already exists (no scaffold needed).
    roster_path = (REPO_ROOT / roster_file) if not Path(roster_file).is_absolute() \
        else Path(roster_file)
    if not roster_path.exists():
        template = REPO_ROOT / "config" / "roster.example.pull.yaml"
        if template.is_file():
            roster_path.parent.mkdir(parents=True, exist_ok=True)
            roster_path.write_text(template.read_text())
            print(f"scaffolded roster {roster_path} (from roster.example.pull.yaml — "
                  "edit teams/repos before your first real run)")
        else:
            print(f"warning: roster {roster_path} missing and no template to scaffold "
                  "from — create it before running", file=sys.stderr)

    try:
        created = _ensure_database(database_url)
        print("created database" if created else "database already exists")
    except Exception as exc:  # noqa: BLE001
        print(f"database setup failed: {exc}", file=sys.stderr)
        return 1

    db.close_pool()
    new_settings = load_settings(instance=project)
    applied = db.migrate(new_settings)
    print(f"migrations: {len(applied)} applied" if applied else "migrations: up to date")
    pool = db.get_pool(new_settings)
    for team, function in agent_plan:
        a = repo.register_agent(pool, team, function, args.runtime)
        print(f"registered agent {a.id}: {a.team}/{a.function}")

    print("\nnext steps:")
    print(f"  1. render each worker's docs:  orchestrator -i {project} render-agent-docs "
          "--team <team> --function <dev|qa> --agent-id <id> --out-dir <worktree>")
    print(f"  2. add a first goal:           orchestrator -i {project} add-goal "
          f"--pipeline {new_settings.default_pipeline} --title \"...\"")
    print("  3. launch the engine + workers.")
    print("\npreflight (orchestrator doctor):")
    code, report = onboarding.summarize(_run_doctor_checks(new_settings))
    print(report)
    return code


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
    # Defaults None → fall back to settings (DASHBOARD_HOST/DASHBOARD_PORT in .env).
    sd.add_argument("--host", default=None)
    sd.add_argument("--port", type=int, default=None)
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

    cla = sub.add_parser("contracts-lifecycle-apply",
                         help="apply an audited, idempotent contract lifecycle batch")
    cla.add_argument("--op", required=True, help="operation_id (idempotency key)")
    cla.add_argument("--file", required=True, help="path to the JSON change batch")
    cla.add_argument("--project", default="cadencelms-working",
                     help="audited project passthrough (must match the configured project)")
    cla.add_argument("--reason", default="", help="human reason recorded in the audit")
    cla.add_argument("--confirm-project", dest="confirm_project", default="",
                     help="type the project name to confirm destructive batches")
    cla.set_defaults(func=_cmd_contracts_lifecycle_apply)

    cd = sub.add_parser("contracts-drift",
                        help="read-only registry vs routes/audit/frontend drift report")
    cd.add_argument("--json", action="store_true", help="emit the full report as JSON")
    cd.add_argument("--strict", action="store_true",
                    help="exit 1 when blocking findings exist (CI gate)")
    cd.add_argument("--severity", choices=("blocking", "advisory", "all"),
                    default="all", help="filter printed findings (default: all)")
    cd.set_defaults(func=_cmd_contracts_drift)

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

    sp = sub.add_parser("setup-project",
                        help="scaffold a workspace, child worktrees, and launcher scripts")
    sp.add_argument("--workspace", required=True,
                    help="workspace parent dir where launchers and worktrees should be written")
    sp.add_argument("--project", default=None,
                    help="orchestrator instance/project name (default: --instance or workspace basename)")
    sp.add_argument("--orchestrator-path", default=str(REPO_ROOT),
                    help="path to this orchestrator project (default: current install)")
    sp.add_argument("--dashboard-url", default="http://127.0.0.1:8800",
                    help="dashboard base URL")
    sp.add_argument("--worktree-prefix", default="wt-",
                    help="prefix for child worker worktree dirs (default: wt-)")
    sp.add_argument("--humantest-worktree", default="humantest-wt",
                    help="worktree dir used for human-test consolidation")
    sp.add_argument("--decomposition-tier", default=decomposition.DEFAULT_TIER,
                    choices=decomposition.tier_names(),
                    help="capability tier that scales issue granularity, per-issue "
                         "internal parallelism, and mid-run drift checks to the fleet's "
                         f"models (default: {decomposition.DEFAULT_TIER}). high=coarse "
                         "feature-slices + internal parallelism; mid=one deliverable "
                         "per issue; remedial=smallest steps + mid-run drift advisories.")
    sp.add_argument("--force", action="store_true",
                    help="overwrite existing launcher files")
    sp.add_argument("--dry-run", action="store_true",
                    help="show files and worktrees that would be written")
    sp.set_defaults(func=_cmd_setup_project)

    ip = sub.add_parser("init",
                        help="guided project bootstrap: instance config + DB + "
                             "migrations + agents + preflight")
    ip.add_argument("--project", required=True, help="project/instance name")
    ip.add_argument("--label", default=None, help="human label (default: titled project)")
    ip.add_argument("--database-url", default=None,
                    help="Postgres URL (default: postgresql://orchestrator:orchestrator"
                         "@localhost:5432/<project>). Created if it does not exist.")
    ip.add_argument("--roster-file", default="config/roster.yaml",
                    help="roster file for this instance (default: config/roster.yaml)")
    ip.add_argument("--decomposition-tier", default=None,
                    choices=decomposition.tier_names(),
                    help="capability tier; omit to recommend one from --models")
    ip.add_argument("--models", default="",
                    help="comma-separated model names the fleet runs; used to "
                         "recommend a decomposition tier when --decomposition-tier is omitted")
    ip.add_argument("--teams", default="backend,frontend",
                    help="comma-separated teams to register dev+qa agents for "
                         "(default: backend,frontend)")
    ip.add_argument("--runtime", default="api", help="agent runtime (default: api)")
    ip.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ip.add_argument("--force", action="store_true",
                    help="overwrite an existing instance drop-in config")
    ip.add_argument("--dry-run", action="store_true",
                    help="print the plan and the config that would be written; make no changes")
    ip.set_defaults(func=_cmd_init)

    dp = sub.add_parser("doctor",
                        help="preflight: validate an instance is ready to launch "
                             "(DB, migrations, roster, pipelines, agents, reasoner)")
    dp.set_defaults(func=_cmd_doctor)

    wf = sub.add_parser("workflow-explain",
                        help="print the effective workflow profile with action authorization verdicts")
    wf.add_argument("--team", default=None,
                    help="team name (resolves worktree from verify_worktrees if present, else CWD)")
    wf.add_argument("--role", default=None,
                    help="role name (default: qa if not given)")
    wf.set_defaults(func=_cmd_workflow_explain)

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
