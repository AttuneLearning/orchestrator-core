"""The single-threaded engine tick.

One tick() performs, in order:
  1. ingest pending inbound messages (triage → local issues, per team)
  2. ingest open goals (backlog → planning) and decompose into issues
  3. assign queued issues to idle agents
  4. advance each active issue exactly one step (work, then gate review)
  5. focus + off-rails sweep
  6. re-engage exhausted issues
  7. reconcile goals (done / paused)

Every issue is processed inside try/except so a single failure cannot halt the
tick. All state changes go through repository.py, so the issue_events log stays
complete. Postgres MVCC keeps a single-threaded loop consistent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from psycopg_pool import ConnectionPool

from .. import adr_rules
from .. import decomposition
from .. import repository as repo
from ..agents.base import GateReview, IssueSpec
from ..config import Settings
from ..models import GoalState, Issue, IssueState
from ..pipelines import Pipeline, first_gate, load_pipelines
from ..roster import load_roster
from ..state_machine import apply_gate_decision, validate_transition
from ..agents.api_worker import ApiWorker
from ..agents.cli_session import CliSessionWorker
from ..agents.reasoning import Reasoner, make_reasoner
from . import focus, offrails, reengagement

_ACTIVE_STATES = [
    IssueState.BACKLOG.value,
    IssueState.READY.value,
    IssueState.IN_PROGRESS.value,
    IssueState.IN_REVIEW.value,
    IssueState.BLOCKED.value,
]
_TERMINAL = {IssueState.DONE.value, IssueState.FAILED.value, IssueState.OFF_RAILS.value,
             IssueState.CANCELLED.value}
# States whose issues are still "live" work (a goal with any of these is not yet
# settled, so its redundant siblings are not auto-cancelled).
_LIVE = set(_ACTIVE_STATES)

# work_types whose issues consume API endpoints and therefore go through the
# contract_check gate (when contract_gate_enabled). Other kinds pass straight
# through. detect_work_type tags 'new-endpoint' for api/route/endpoint work.
CONTRACT_WORK_TYPES = {"new-endpoint"}

# Teams whose inbound messages are NOT auto-decomposed into worker issues — they
# queue pending for the human-reviewed /orch/monitor dashboard instead.
MONITOR_TEAMS = {"orchestration"}


@dataclass
class TickSummary:
    ingested: int = 0
    rejected: int = 0
    decomposed: int = 0
    subissues: int = 0
    unblocked: int = 0
    contract_blocked: int = 0
    alerts: int = 0
    cancelled: int = 0
    adr_proposals: int = 0
    assigned: int = 0
    advanced: int = 0
    completed: int = 0
    failed: int = 0
    quarantined: int = 0
    reengaged: int = 0
    reclaimed: int = 0
    goals_done: int = 0
    goals_paused: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return any([self.ingested, self.rejected, self.decomposed, self.subissues,
                    self.unblocked, self.contract_blocked, self.alerts,
                    self.cancelled, self.adr_proposals,
                    self.assigned, self.advanced, self.completed, self.failed,
                    self.quarantined, self.reengaged, self.reclaimed,
                    self.goals_done, self.goals_paused])


class Engine:
    def __init__(
        self,
        settings: Settings,
        pool: ConnectionPool,
        reasoner: Optional[Reasoner] = None,
        worker: Optional[ApiWorker] = None,
    ):
        self.settings = settings
        self.pool = pool
        self.reasoner = reasoner or make_reasoner(settings)
        self.worker = worker or ApiWorker(settings)
        self.cli_worker = CliSessionWorker(settings)
        self.pipelines = load_pipelines(settings.pipelines)
        self.roster = load_roster(settings.roster)
        self.t = settings.thresholds

    # -- helpers ------------------------------------------------------------ #

    def _pipeline(self, issue: Issue) -> Pipeline:
        return self.pipelines.get(issue.pipeline) or self.pipelines[self.settings.default_pipeline]

    def _ingest_pipeline_for(self, team_id: str) -> str:
        """Pipeline a comms-ingested goal for `team_id` should run on: the team's
        pull pipeline (so a live coder works the cross-team request) if one exists,
        else the configured default. Without this, ingested goals fell back to the
        verdict pipeline-1 and auto-failed (the reasoner can't implement)."""
        for name, pl in self.pipelines.items():
            if pl.team == team_id:
                return name
        return self.settings.default_pipeline

    def _transition(self, issue: Issue, to_state: str, *, gate_type=None,
                    event_type="state_change", payload=None,
                    retry_count=None, step_count=None) -> Issue:
        if not validate_transition(issue.state, to_state):
            repo.append_log(self.pool, issue.id, "error",
                            {"illegal_transition": f"{issue.state}->{to_state}"})
            raise ValueError(f"illegal transition {issue.state}->{to_state}")
        return repo.update_state(
            self.pool, issue.id, to_state, gate_type=gate_type, event_type=event_type,
            payload=payload, retry_count=retry_count, step_count=step_count,
        )

    def _release_agent(self, issue: Issue) -> None:
        if issue.assigned_agent is not None:
            repo.set_agent_status(self.pool, issue.assigned_agent, "idle")

    def _alert(self, summary: TickSummary, *, goal_id: int,
               issue_id: Optional[int] = None, hold: bool = False, **detail) -> None:
        """Raise an operator alert (decomposition cap / routing-invariant breach).

        Logged as an 'alert' event so it surfaces in the timeline; when hold=True
        the goal is paused so it lands in get_alerts' paused_goals attention set
        (no silent truncation — the operator decides). Anchors to a representative
        issue when one exists, else the goal's first issue (events are per-issue)."""
        anchor = issue_id
        if anchor is None:
            siblings = repo.list_issues(self.pool, goal_id=goal_id)
            anchor = siblings[0].id if siblings else None
        if anchor is not None:
            repo.append_log(self.pool, anchor, "alert", dict(detail, goal_id=goal_id))
        if hold:
            repo.set_goal_state(self.pool, goal_id, GoalState.PAUSED.value)
        summary.alerts += 1

    def _implementation_worker(self, issue: Issue):
        """Pick the worker by the assigned agent's runtime (api default)."""
        if issue.assigned_agent is not None:
            agent = repo.get_agent(self.pool, issue.assigned_agent)
            if agent is not None and agent.runtime == "cli":
                return self.cli_worker
        return self.worker

    def _idle_worker_for(self, issue: Issue, gate):
        """Find an agent to own `gate`. A pull gate requires an idle EXTERNAL
        worker of the gate's owner function (the engine never works it). A verdict
        gate takes an owner-function agent, else any team agent — a capacity token
        for the reasoner/human verdict."""
        if gate.mode == "pull":
            return repo.find_idle_agent(self.pool, issue.team, gate.owner,
                                        runtime="external")
        return (repo.find_idle_agent(self.pool, issue.team, gate.owner)
                or repo.find_idle_agent(self.pool, issue.team))

    def _applicable_rules(self, issue: Issue) -> list[dict]:
        """Accepted ADR rules matching this issue (work-type ∩ team ∩ repos).

        self._tick_rules is loaded once per tick; the per-issue selection is
        recomputed on every call, so rule changes apply from the next tick —
        that's the 'refreshed as needed' contract."""
        team = self.roster.resolve(issue.team)
        repos = list(team.repos) if team else []
        return adr_rules.applicable(
            self._tick_rules, work_type=issue.work_type,
            team=issue.team, repos=repos,
        )

    def _call_with_rules(self, fn, *args, rules: str):
        """Invoke a reasoner op, tolerating reasoners that predate the rules
        parameter (optional capability, like assess_complexity/triage)."""
        try:
            return fn(*args, rules=rules)
        except TypeError:
            return fn(*args)

    def _comms_respond(self, issue: Issue) -> None:
        """comms_response gate work: answer the originating team and archive the
        original (PROCESS_GUIDE phase 5 — NOT optional when message-triggered)."""
        if issue.origin_message_id is None:
            return  # message-triggered without a tracked origin; nothing to answer
        origin = repo.get_message(self.pool, issue.origin_message_id)
        if origin is None:
            return
        repo.create_message(
            self.pool, from_team=issue.team, to_team=origin["from_team"],
            subject=f"Re: {origin['subject']}",
            body=(f"Completed issue #{issue.id}: {issue.title}. "
                  "See the issue event log for what changed."),
            priority=origin.get("priority", "medium"),
            issue_id=issue.id, kind="response", status="sent",
        )
        repo.archive_message(self.pool, origin["id"])
        repo.append_log(self.pool, issue.id, "comms_response",
                        {"to": origin["from_team"],
                         "origin_message_id": origin["id"]})

    # -- tick phases -------------------------------------------------------- #

    def _ingest(self, summary: TickSummary) -> None:
        """Triage pending inbound requests into local issues.

        Issues stay local: a message to team X only ever creates an issue owned
        by team X (protocol.yaml). Each accepted message gets its own goal so
        goal reconciliation closes the loop when the issue completes. Responses
        (kind='response') are never ingested, so two teams can't ping-pong."""
        for msg in repo.pending_messages(self.pool):
            try:
                team = self.roster.resolve(msg["to_team"])
                if team is None:
                    repo.triage_message(self.pool, msg["id"], accept=False,
                                        reason=f"unknown team {msg['to_team']!r}")
                    summary.rejected += 1
                    continue
                if team.id in MONITOR_TEAMS:
                    # Orchestration-monitor inbox: process/architecture questions
                    # are never auto-decomposed into worker issues. They stay
                    # pending for the human-reviewed /orch/monitor dashboard.
                    continue
                triage = getattr(self.reasoner, "triage_message", None)
                if triage is None:
                    continue  # optional capability: leave message pending
                decision = triage(msg)
                if decision.accept:
                    pipeline = self._ingest_pipeline_for(team.id)
                    goal = repo.create_goal(
                        self.pool, f"[comms] {msg['subject']}", msg.get("body", ""),
                        pipeline=pipeline,
                    )
                    repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
                    repo.create_issue(
                        self.pool, goal.id,
                        decision.title or msg["subject"], decision.description,
                        team=team.id, pipeline=pipeline, triggered_by_message=True,
                        origin_message_id=msg["id"],
                    )
                    repo.triage_message(self.pool, msg["id"], accept=True)
                    summary.ingested += 1
                else:
                    repo.triage_message(self.pool, msg["id"], accept=False,
                                        reason=decision.reason)
                    summary.rejected += 1
            except Exception as exc:  # noqa: BLE001 - isolate message failures
                summary.errors.append(f"ingest message {msg['id']}: {exc}")

    def _decompose(self, summary: TickSummary) -> None:
        for goal in repo.list_open_goals(self.pool):
            if goal.kind == "maintenance":
                continue  # a standing backlog container — never reasoner-decomposed
            if goal.state == GoalState.BACKLOG.value:
                repo.set_goal_state(self.pool, goal.id, GoalState.PLANNING.value)
                goal.state = GoalState.PLANNING.value
            if goal.state != GoalState.PLANNING.value:
                continue
            if repo.count_issues_for_goal(self.pool, goal.id) > 0:
                repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
                continue
            try:
                pid = goal.pipeline if goal.pipeline in self.pipelines \
                    else self.settings.default_pipeline
                pipeline = self.pipelines[pid]
                mode = decomposition.decompose_mode(
                    goal.decompose, goal.title, goal.description)
                specs = self.reasoner.decompose_goal(goal, self.t.max_subissues)
                # Drop candidates that merely duplicate a QA gate — the runner owns
                # verification; it is acceptance criteria on the impl issue, not its
                # own issue (no standalone test/typecheck/e2e/bundle-output issues).
                specs = decomposition.drop_qa_duplicates(specs)
                if mode == decomposition.SINGLE:
                    # Simple goal / explicit override: exactly one implementation
                    # issue. Synthesize one if the filter emptied the list.
                    specs = specs[:1] or [IssueSpec(
                        title=f"Implement: {goal.title}", description=goal.description)]
                if len(specs) > self.t.max_issues_per_goal:
                    # Over-decomposition blowout: alert + hold rather than mint a
                    # pile of unpullable issues (spec §3.1 — no silent truncation).
                    self._alert(summary, goal_id=goal.id, hold=True,
                                cap="max_issues_per_goal",
                                wanted=len(specs), limit=self.t.max_issues_per_goal)
                    continue
                # Pipeline team wins: children inherit it and never re-derive team
                # from issue text (the text inference misroutes pull-fe → backend).
                created: list[tuple[int, str]] = []
                for spec in specs:
                    team_name = pipeline.team or spec.team
                    team = self.roster.resolve(team_name)
                    team_id = team.id if team else team_name
                    issue = repo.create_issue(self.pool, goal.id, spec.title,
                                              spec.description, team=team_id, pipeline=pid)
                    created.append((issue.id, team_id))
                    summary.decomposed += 1
                # Routing invariant: every child must resolve to a known team, and
                # match the pipeline team when one is declared. A violation alerts +
                # holds the goal rather than emitting misrouted/unpullable work.
                violations = decomposition.routing_violations(
                    created, pipeline.team, self.roster.resolve)
                if violations:
                    self._alert(summary, goal_id=goal.id, hold=True,
                                invariant="routing", violations=violations)
                    continue
                repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
            except Exception as exc:  # noqa: BLE001 - isolate goal failures
                summary.errors.append(f"decompose goal {goal.id}: {exc}")

    def _maybe_decompose(self, issue: Issue, summary: TickSummary) -> bool:
        """Architect check: split an oversized issue into sub-issues and block the
        parent on them. Called exactly once per issue (when it leaves backlog).
        Returns True if the issue was decomposed. Caps bound the recursion:
        depth (MAX_DEPTH), children per split (MAX_SUBISSUES), and total issues
        per goal (MAX_ISSUES_PER_GOAL)."""
        if issue.depth >= self.t.max_depth:
            return False
        # A simple goal (or explicit single override) is never split — that fast-
        # path is the primary guard against the over-decomposition blowout.
        goal = repo.get_goal(self.pool, issue.goal_id)
        if goal is not None and decomposition.decompose_mode(
                goal.decompose, goal.title, goal.description) == decomposition.SINGLE:
            return False
        # Optional capability: reasoners without an architect op never decompose.
        assess = getattr(self.reasoner, "assess_complexity", None)
        if assess is None:
            return False
        assessment = assess(issue)
        if not assessment.decompose:
            return False
        # Architect output is implementation work only — drop QA-gate duplicates.
        wanted_specs = decomposition.drop_qa_duplicates(assessment.subissues)
        if not wanted_specs:
            return False
        room = self.t.max_issues_per_goal - repo.count_issues_for_goal(self.pool, issue.goal_id)
        if room <= 0:
            # Per-goal cap reached: alert (don't silently drop work) but let this
            # issue proceed undecomposed — it's legitimate work, not a blowout. The
            # heavier hold-the-goal response is reserved for the _decompose blowout.
            self._alert(summary, goal_id=issue.goal_id, issue_id=issue.id,
                        cap="max_issues_per_goal", wanted=len(wanted_specs))
            return False
        n = min(len(wanted_specs), self.t.max_subissues,
                self.t.max_children_per_parent, room)
        if len(wanted_specs) > n:
            # Wanted more than the width/depth caps allow — record it (not silent).
            self._alert(summary, goal_id=issue.goal_id, issue_id=issue.id,
                        cap="max_children_per_parent",
                        wanted=len(wanted_specs), created=n)
        children = [
            repo.create_subissue(self.pool, issue, spec.title, spec.description)
            for spec in wanted_specs[:n]
        ]
        self._transition(issue, IssueState.BLOCKED.value, event_type="decomposed",
                         payload={"children": [c.id for c in children]})
        summary.subissues += len(children)
        return True

    def _maybe_suggest_adr(self, issue: Issue, summary: TickSummary) -> None:
        """Gap detection: an issue completed with no governing rules — ask the
        reasoner (optional capability) whether a reusable decision should exist.
        Drafts land as status='proposed' (inert until a human approves)."""
        suggest = getattr(self.reasoner, "suggest_adr", None)
        if suggest is None:
            return
        try:
            # Dedup: if a pending proposal already covers these coordinates,
            # don't pile on another one for the human to wade through.
            team = self.roster.resolve(issue.team)
            pending = adr_rules.applicable(
                [dict(r, status="accepted")  # treat proposals as live for matching
                 for r in repo.list_adrs(self.pool, status="proposed")],
                work_type=issue.work_type, team=issue.team,
                repos=list(team.repos) if team else [],
            )
            if pending:
                return
            draft = suggest(issue)
            if not draft:
                return
            adr = repo.create_adr(
                self.pool, draft["domain"], draft.get("title", issue.title),
                decision=draft["decision"], context=draft.get("context", ""),
                applies_to=draft.get("applies_to") or {},
                status="proposed",
                proposed_by=f"agent:{issue.assigned_agent or 'reasoner'}",
            )
            repo.append_log(self.pool, issue.id, "adr_proposed",
                            {"adr_key": adr["adr_key"]})
            summary.adr_proposals += 1
        except Exception as exc:  # noqa: BLE001 - gap detection must never block flow
            summary.errors.append(f"suggest_adr issue {issue.id}: {exc}")

    def _unblock(self, summary: TickSummary) -> None:
        """Resolve decomposed parents: all children done → ready; any child
        failed/off_rails → the parent fails too (it cannot proceed), which lets
        goal reconciliation pause the goal for human attention."""
        for issue in repo.list_issues(self.pool, states=[IssueState.BLOCKED.value]):
            try:
                children = repo.list_issues(self.pool, parent_id=issue.id)
                if not children:
                    # Not decomposition — maybe blocked-by-contract (contract_check).
                    # Release as soon as every needed endpoint has an agreed/live
                    # contract; backend keeps building toward 'live' in parallel.
                    deps = repo.list_issue_contract_deps(self.pool, issue.id)
                    if deps and all(
                        repo.contract_satisfied(self.pool, d["method"], d["path"])
                        for d in deps
                    ):
                        repo.mark_contract_deps_satisfied(self.pool, issue.id)
                        self._transition(
                            issue, IssueState.READY.value,
                            payload={"unblocked_by_contracts":
                                     [f"{d['method']} {d['path']}" for d in deps]})
                        summary.unblocked += 1
                    continue  # otherwise blocked for some other reason; not ours
                states = {c.state for c in children}
                states = {c.state for c in children}
                if states <= {IssueState.DONE.value}:
                    self._transition(issue, IssueState.READY.value,
                                     payload={"unblocked_by": [c.id for c in children]})
                    summary.unblocked += 1
                elif states & {IssueState.FAILED.value, IssueState.OFF_RAILS.value}:
                    self._transition(issue, IssueState.FAILED.value,
                                     payload={"failed_children": [
                                         c.id for c in children
                                         if c.state in (IssueState.FAILED.value,
                                                        IssueState.OFF_RAILS.value)]})
                    summary.failed += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"unblock issue {issue.id}: {exc}")

    def _run_contract_check(self, issue: Issue, summary: TickSummary) -> bool:
        """Contract-first triage for an endpoint-consuming issue. Returns True if
        the issue was BLOCKED on missing contracts (caller skips the review
        transition); False means the gate passes (proceed to review → next gate).

        Opt-in (contract_gate_enabled) and scoped to CONTRACT_WORK_TYPES; the
        endpoint-extraction reasoner op is optional (absent → no-op pass, so old
        reasoners keep working)."""
        if not getattr(self.settings, "contract_gate_enabled", False):
            return False
        if issue.work_type not in CONTRACT_WORK_TYPES:
            return False
        extract = getattr(self.reasoner, "extract_endpoint_deps", None)
        if extract is None:
            return False
        deps = extract(issue) or []
        missing = [d for d in deps
                   if not repo.contract_satisfied(self.pool, d["method"], d["path"])]
        if not missing:
            return False
        # Block: ask backend (one message) for the missing contracts and record
        # the deps; _unblock releases the issue once each reaches agreed/live.
        for d in missing:
            repo.propose_contract(self.pool, d["method"], d["path"], owner_team="backend")
        listing = ", ".join(f"{d['method']} {d['path']}" for d in missing)
        repo.create_message(
            self.pool, from_team=issue.team, to_team="backend",
            subject=f"Contract(s) needed for issue #{issue.id}",
            body=(f"Issue #{issue.id} ({issue.title}) consumes: {listing}. Please "
                  "agree each contract (contract_agree) so the frontend can build "
                  "against the shape, then implement the endpoint(s)."),
            priority="high", issue_id=issue.id, kind="request",
        )
        repo.add_issue_contract_deps(self.pool, issue.id, missing)
        self._transition(
            issue, IssueState.BLOCKED.value, gate_type=issue.gate_type,
            event_type="contract_blocked", payload={"missing": listing.split(", ")},
        )
        summary.contract_blocked += 1
        return True

    def _assign(self, summary: TickSummary) -> None:
        for issue in repo.list_issues(self.pool, states=[IssueState.BACKLOG.value]):
            try:
                # tag work-type (drives ADR rule selection), plan under the
                # applicable rules, then either decompose (architect) or mark ready
                if issue.work_type is None:
                    issue.work_type = adr_rules.detect_work_type(
                        f"{issue.title} {issue.description}")
                    repo.set_work_type(self.pool, issue.id, issue.work_type)
                rules = self._applicable_rules(issue)
                block = adr_rules.format_rules_block(rules)
                plan = self._call_with_rules(self.reasoner.plan_issue, issue,
                                             rules=block)
                repo.append_log(self.pool, issue.id, "plan",
                                {"plan": plan, "work_type": issue.work_type,
                                 "rules": [r["adr_key"] for r in rules]})
                if self._maybe_decompose(issue, summary):
                    continue
                issue = self._transition(issue, IssueState.READY.value)
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"plan issue {issue.id}: {exc}")

        # Maintenance backfill: standard work claims idle workers first; a
        # maintenance issue is assignable only when its team has no standard work
        # ready or in flight (focus.select_assignable, pure).
        ready = repo.list_issues(self.pool, states=[IssueState.READY.value])
        in_progress = repo.list_issues(self.pool, states=[IssueState.IN_PROGRESS.value])
        maint_ids = repo.maintenance_goal_ids(self.pool)
        for issue in focus.select_assignable(ready, in_progress, maint_ids):
            try:
                gate = first_gate(self._pipeline(issue),
                                  triggered_by_message=issue.triggered_by_message)
                if gate is None:
                    self._transition(issue, IssueState.DONE.value, event_type="gate_pass")
                    summary.completed += 1
                    continue
                agent = self._idle_worker_for(issue, gate)
                if agent is None:
                    continue  # no capacity this tick; remains READY
                repo.claim_issue(self.pool, issue.id, agent.id)
                self._transition(issue, IssueState.IN_PROGRESS.value, gate_type=gate.type,
                                 event_type="gate_enter")
                summary.assigned += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"assign issue {issue.id}: {exc}")

        # Pull-gate (re)assignment: an issue advanced (by an external worker's
        # gate_decision) into a pull gate keeps its previous gate's agent. Hand it
        # to an idle external worker of the new gate's owner — releasing the
        # carried-over agent — so dev→qa pull handoffs route correctly. Issues
        # already owned by the right live external worker are left alone (it's
        # working them out-of-band).
        for issue in repo.list_issues(self.pool, states=[IssueState.IN_PROGRESS.value]):
            try:
                gate = self._pipeline(issue).gate(issue.gate_type or "")
                if gate is None or gate.mode != "pull":
                    continue
                current = (repo.get_agent(self.pool, issue.assigned_agent)
                           if issue.assigned_agent is not None else None)
                if (current is not None and current.runtime == "external"
                        and current.function == gate.owner):
                    continue  # correctly owned by a live external worker
                worker = repo.find_idle_agent(self.pool, issue.team, gate.owner,
                                              runtime="external")
                if worker is None:
                    # No eligible external worker: release any carried-over agent
                    # and leave the gate unassigned, waiting. (liveness handles a
                    # worker that claims then dies.)
                    if current is not None:
                        self._release_agent(issue)
                        repo.unassign_issue(self.pool, issue.id)
                    continue
                self._release_agent(issue)
                repo.claim_issue(self.pool, issue.id, worker.id)
                repo.append_log(self.pool, issue.id, "gate_enter",
                                {"reassigned_to": worker.id, "gate": gate.type})
                summary.assigned += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"reassign issue {issue.id}: {exc}")

    def _advance(self, summary: TickSummary) -> None:
        # Work step: IN_PROGRESS issues do their gate's work, then enter review.
        for issue in repo.list_issues(self.pool, states=[IssueState.IN_PROGRESS.value]):
            try:
                gate = self._pipeline(issue).gate(issue.gate_type or "")
                if issue.gate_type == "contract_check":
                    # Mechanical, contract-first triage: pass through to review when
                    # every consumed endpoint has an agreed/live contract; otherwise
                    # request the missing contracts and block (handled in-place).
                    if self._run_contract_check(issue, summary):
                        continue
                if gate is not None and gate.mode == "pull":
                    # Pull gate: a live external worker owns the repo work and
                    # drives the transition via MCP (report_work + gate_decision).
                    # The engine is hands-off — no work, no review, no transition.
                    continue
                if issue.gate_type == "implementation":
                    self._implementation_worker(issue).implement(self.pool, issue)
                elif issue.gate_type == "qa_gate" and self.settings.apply_enabled:
                    # apply/verify leg (flag-off by default): worktree + verify,
                    # result logged for the gate reviewer. Never merges.
                    from ..apply.worktree import apply_and_verify
                    apply_and_verify(self.pool, issue, self.settings)
                elif issue.gate_type == "comms_response":
                    self._comms_respond(issue)
                if issue.assigned_agent is not None:
                    repo.touch_agent(self.pool, issue.assigned_agent)
                self._transition(
                    issue, IssueState.IN_REVIEW.value, gate_type=issue.gate_type,
                    event_type="gate_enter", step_count=issue.step_count + 1,
                )
                summary.advanced += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"work issue {issue.id}: {exc}")
                repo.append_log(self.pool, issue.id, "error", {"phase": "work", "error": str(exc)})

        # Review step: IN_REVIEW issues get a gate_review and transition.
        for issue in repo.list_issues(self.pool, states=[IssueState.IN_REVIEW.value]):
            try:
                pipeline = self._pipeline(issue)
                gate = pipeline.gate(issue.gate_type)
                # Verdict evidence: include the payloads of pull-worker reports
                # (what the coder committed, what the QA runner found) so the
                # reviewer judges the actual work, not just the event sequence.
                _evidence = {"code_committed", "tests_run", "verification"}
                recent = [
                    ({"event_type": e.event_type, "to_state": e.to_state,
                      "payload": e.payload} if e.event_type in _evidence
                     else {"event_type": e.event_type, "to_state": e.to_state})
                    for e in repo.recent_events(self.pool, issue.id, limit=20)
                ]
                rules = self._applicable_rules(issue)
                if issue.gate_type == "contract_check":
                    # The work phase already decided (passed, else it blocked and
                    # never reached review) — auto-pass without a reasoner call.
                    review = GateReview(passed=True, reasons=["contracts satisfied"])
                elif issue.gate_type == "intake":
                    # Intake is lightweight admission, NOT an ADR-compliance verdict.
                    # ADR governance belongs at gates where the work exists (qa_gate/
                    # completion); judging a not-yet-implemented issue's description
                    # against rules like "ships matching tests" is structurally
                    # premature (the tests can't exist yet) and rejects valid work.
                    # Admit without a reasoner call — applies to every pipeline's intake.
                    review = GateReview(passed=True, reasons=["admitted"])
                else:
                    review = self._call_with_rules(
                        self.reasoner.gate_review, issue, issue.gate_type, recent,
                        rules=adr_rules.format_rules_block(rules),
                    )
                outcome = apply_gate_decision(
                    pipeline, gate, passed=review.passed,
                    retry_count=issue.retry_count, retry_cap=self.t.retry_cap,
                    triggered_by_message=issue.triggered_by_message,
                )
                payload = {"reasons": review.reasons}
                if getattr(review, "violated_rules", None):
                    payload["violated_rules"] = review.violated_rules
                self._transition(
                    issue, outcome.state, gate_type=outcome.gate_type,
                    event_type=outcome.event_type,
                    payload=payload,
                    retry_count=outcome.retry_count,
                )
                if outcome.state == IssueState.DONE.value:
                    self._release_agent(issue)
                    summary.completed += 1
                    if not rules:
                        self._maybe_suggest_adr(issue, summary)
                elif outcome.state == IssueState.FAILED.value:
                    self._release_agent(issue)
                    summary.failed += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"review issue {issue.id}: {exc}")
                repo.append_log(self.pool, issue.id, "error", {"phase": "review", "error": str(exc)})

    def _sweep(self, summary: TickSummary) -> None:
        active = repo.list_issues(self.pool, states=_ACTIVE_STATES)
        for issue in active:
            try:
                events = repo.recent_events(self.pool, issue.id, limit=200)
                # signals_after_directive ignores events before the latest human
                # directive, so a resumed issue gets a genuinely fresh start.
                signals = focus.signals_after_directive(issue, events, self.t)
                if not signals:
                    continue
                drift = self.reasoner.score_drift(
                    issue, [{"event_type": e.event_type} for e in events]
                )
                repo.append_log(self.pool, issue.id, "drift_score",
                                {"signals": signals, "drift": drift})
                if offrails.should_quarantine(signals, drift, self.t.drift_threshold):
                    self._transition(issue, IssueState.OFF_RAILS.value,
                                     gate_type=issue.gate_type, event_type="state_change",
                                     payload={"signals": signals, "drift": drift})
                    self._release_agent(issue)
                    repo.set_goal_state(self.pool, issue.goal_id, GoalState.PAUSED.value)
                    summary.quarantined += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"sweep issue {issue.id}: {exc}")

    def _reclaim(self, summary: TickSummary) -> None:
        """Liveness for pull gates: a pull-gate issue whose external worker hasn't
        been seen within agent_stale_seconds is reclaimed — the dead worker is
        marked offline and the issue unassigned, so the _assign reassign scan can
        hand it to a fresh worker. After reclaim_cap reclaims the issue is
        quarantined (off_rails), matching the autonomous focus/off-rails latch."""
        for issue in repo.list_issues(self.pool, states=[IssueState.IN_PROGRESS.value]):
            try:
                gate = self._pipeline(issue).gate(issue.gate_type or "")
                if (gate is None or gate.mode != "pull"
                        or issue.assigned_agent is None):
                    continue
                age = repo.agent_seconds_since_seen(self.pool, issue.assigned_agent)
                if age is not None and age <= self.t.agent_stale_seconds:
                    continue  # worker is alive
                dead = issue.assigned_agent
                n = sum(1 for e in repo.recent_events(self.pool, issue.id, limit=200)
                        if e.event_type == "reclaimed") + 1
                repo.set_agent_status(self.pool, dead, "offline")
                repo.unassign_issue(self.pool, issue.id)
                repo.append_log(self.pool, issue.id, "reclaimed",
                                {"agent": dead, "count": n, "stale_seconds": age})
                if n >= self.t.reclaim_cap:
                    self._transition(issue, IssueState.OFF_RAILS.value,
                                     gate_type=issue.gate_type, event_type="state_change",
                                     payload={"reason": "reclaim_cap", "reclaims": n})
                    repo.set_goal_state(self.pool, issue.goal_id, GoalState.PAUSED.value)
                    summary.quarantined += 1
                else:
                    summary.reclaimed += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"reclaim issue {issue.id}: {exc}")

    def _reengage(self, summary: TickSummary) -> None:
        for issue in repo.list_issues(
            self.pool, states=[IssueState.IN_PROGRESS.value, IssueState.IN_REVIEW.value]
        ):
            try:
                if (reengagement.is_exhausted(issue, self.t.step_budget)
                        and not reengagement.already_reengaged(self.pool, issue.id)):
                    reengagement.reengage(self.pool, issue)
                    summary.reengaged += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"reengage issue {issue.id}: {exc}")

    def _reconcile(self, summary: TickSummary) -> None:
        for goal in repo.list_open_goals(self.pool):
            if goal.kind == "maintenance":
                continue  # perpetual standing backlog — never auto-complete/pause
            issues = repo.list_issues(self.pool, goal_id=goal.id)
            if not issues:
                continue
            states = {i.state for i in issues}
            if states <= _TERMINAL:
                if IssueState.OFF_RAILS.value in states or IssueState.FAILED.value in states:
                    repo.set_goal_state(self.pool, goal.id, GoalState.PAUSED.value)
                    summary.goals_paused += 1
                else:
                    repo.set_goal_state(self.pool, goal.id, GoalState.DONE.value)
                    summary.goals_done += 1

    # -- public ------------------------------------------------------------- #

    def tick(self) -> TickSummary:
        summary = TickSummary()
        # ADR rules load once per tick: cheap, and edits go live next tick.
        self._tick_rules = repo.list_adrs(self.pool, status="accepted")
        self._ingest(summary)
        self._decompose(summary)
        self._unblock(summary)
        self._assign(summary)
        self._advance(summary)
        self._reclaim(summary)
        self._sweep(summary)
        self._reengage(summary)
        self._reconcile(summary)
        return summary

    def run(self, max_ticks: int = 100,
            on_tick: Optional[Callable[[TickSummary], None]] = None) -> list[TickSummary]:
        """Tick until quiescent (no work performed) or max_ticks reached."""
        history: list[TickSummary] = []
        for _ in range(max_ticks):
            summary = self.tick()
            history.append(summary)
            if on_tick is not None:
                on_tick(summary)
            if not summary.did_work:
                break
        return history

    def run_daemon(self, interval: float = 5.0,
                   on_tick: Optional[Callable[[TickSummary], None]] = None) -> None:
        """Tick forever, sleeping between quiescent ticks. Ctrl-C returns cleanly."""
        try:
            while True:
                try:
                    repo.record_daemon_heartbeat(self.pool)
                except Exception:  # noqa: BLE001 - liveness stamp must never stop the loop
                    pass
                summary = self.tick()
                if on_tick is not None:
                    on_tick(summary)
                if not summary.did_work:
                    time.sleep(interval)
        except KeyboardInterrupt:
            return
