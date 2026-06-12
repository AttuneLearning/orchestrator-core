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
from .. import repository as repo
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
_TERMINAL = {IssueState.DONE.value, IssueState.FAILED.value, IssueState.OFF_RAILS.value}


@dataclass
class TickSummary:
    ingested: int = 0
    rejected: int = 0
    decomposed: int = 0
    subissues: int = 0
    unblocked: int = 0
    adr_proposals: int = 0
    assigned: int = 0
    advanced: int = 0
    completed: int = 0
    failed: int = 0
    quarantined: int = 0
    reengaged: int = 0
    goals_done: int = 0
    goals_paused: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return any([self.ingested, self.rejected, self.decomposed, self.subissues,
                    self.unblocked, self.adr_proposals, self.assigned, self.advanced,
                    self.completed, self.failed, self.quarantined, self.reengaged,
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

    def _implementation_worker(self, issue: Issue):
        """Pick the worker by the assigned agent's runtime (api default)."""
        if issue.assigned_agent is not None:
            agent = repo.get_agent(self.pool, issue.assigned_agent)
            if agent is not None and agent.runtime == "cli":
                return self.cli_worker
        return self.worker

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
                triage = getattr(self.reasoner, "triage_message", None)
                if triage is None:
                    continue  # optional capability: leave message pending
                decision = triage(msg)
                if decision.accept:
                    goal = repo.create_goal(
                        self.pool, f"[comms] {msg['subject']}", msg.get("body", "")
                    )
                    repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
                    repo.create_issue(
                        self.pool, goal.id,
                        decision.title or msg["subject"], decision.description,
                        team=team.id, triggered_by_message=True,
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
            if goal.state == GoalState.BACKLOG.value:
                repo.set_goal_state(self.pool, goal.id, GoalState.PLANNING.value)
                goal.state = GoalState.PLANNING.value
            if goal.state != GoalState.PLANNING.value:
                continue
            if repo.count_issues_for_goal(self.pool, goal.id) > 0:
                repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
                continue
            try:
                specs = self.reasoner.decompose_goal(goal, self.t.max_subissues)
                room = self.t.max_issues_per_goal
                pipeline = goal.pipeline if goal.pipeline in self.pipelines \
                    else self.settings.default_pipeline
                for spec in specs[:room]:
                    team = self.roster.resolve(spec.team)
                    team_id = team.id if team else spec.team
                    repo.create_issue(self.pool, goal.id, spec.title, spec.description,
                                      team=team_id, pipeline=pipeline)
                    summary.decomposed += 1
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
        # Optional capability: reasoners without an architect op never decompose.
        assess = getattr(self.reasoner, "assess_complexity", None)
        if assess is None:
            return False
        assessment = assess(issue)
        if not assessment.decompose:
            return False
        room = self.t.max_issues_per_goal - repo.count_issues_for_goal(self.pool, issue.goal_id)
        n = min(len(assessment.subissues), self.t.max_subissues, max(room, 0))
        if n <= 0:
            repo.append_log(self.pool, issue.id, "error",
                            {"cap": "max_issues_per_goal",
                             "wanted": len(assessment.subissues)})
            return False
        children = [
            repo.create_subissue(self.pool, issue, spec.title, spec.description)
            for spec in assessment.subissues[:n]
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
                    continue  # blocked for some other reason; not ours to resolve
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

        for issue in repo.list_issues(self.pool, states=[IssueState.READY.value]):
            try:
                gate = first_gate(self._pipeline(issue),
                                  triggered_by_message=issue.triggered_by_message)
                if gate is None:
                    self._transition(issue, IssueState.DONE.value, event_type="gate_pass")
                    summary.completed += 1
                    continue
                agent = repo.find_idle_agent(self.pool, issue.team, gate.owner) \
                    or repo.find_idle_agent(self.pool, issue.team)
                if agent is None:
                    continue  # no capacity this tick; remains READY
                repo.claim_issue(self.pool, issue.id, agent.id)
                self._transition(issue, IssueState.IN_PROGRESS.value, gate_type=gate.type,
                                 event_type="gate_enter")
                summary.assigned += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"assign issue {issue.id}: {exc}")

    def _advance(self, summary: TickSummary) -> None:
        # Work step: IN_PROGRESS issues do their gate's work, then enter review.
        for issue in repo.list_issues(self.pool, states=[IssueState.IN_PROGRESS.value]):
            try:
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
                recent = [
                    {"event_type": e.event_type, "to_state": e.to_state}
                    for e in repo.recent_events(self.pool, issue.id, limit=20)
                ]
                rules = self._applicable_rules(issue)
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
                summary = self.tick()
                if on_tick is not None:
                    on_tick(summary)
                if not summary.did_work:
                    time.sleep(interval)
        except KeyboardInterrupt:
            return
