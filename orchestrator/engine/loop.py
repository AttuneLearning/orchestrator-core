"""The single-threaded engine tick.

One tick() performs, in order:
  1. ingest open goals (backlog → planning)
  2. decompose planning goals into issues (respecting caps)
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

from .. import repository as repo
from ..config import Settings
from ..models import GoalState, Issue, IssueState
from ..pipelines import Pipeline, first_gate, load_pipelines
from ..roster import load_roster
from ..state_machine import apply_gate_decision, validate_transition
from ..agents.api_worker import ApiWorker
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
    decomposed: int = 0
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
        return any([self.decomposed, self.assigned, self.advanced, self.completed,
                    self.failed, self.quarantined, self.reengaged,
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

    # -- tick phases -------------------------------------------------------- #

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
                for spec in specs[:room]:
                    team = self.roster.resolve(spec.team)
                    team_id = team.id if team else spec.team
                    repo.create_issue(self.pool, goal.id, spec.title, spec.description,
                                      team=team_id)
                    summary.decomposed += 1
                repo.set_goal_state(self.pool, goal.id, GoalState.ACTIVE.value)
            except Exception as exc:  # noqa: BLE001 - isolate goal failures
                summary.errors.append(f"decompose goal {goal.id}: {exc}")

    def _assign(self, summary: TickSummary) -> None:
        for issue in repo.list_issues(self.pool, states=[IssueState.BACKLOG.value]):
            try:
                # plan (stored on the log for review/re-engagement), then mark ready
                plan = self.reasoner.plan_issue(issue)
                repo.append_log(self.pool, issue.id, "plan", {"plan": plan})
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
                    self.worker.implement(self.pool, issue)
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
                review = self.reasoner.gate_review(issue, issue.gate_type, recent)
                outcome = apply_gate_decision(
                    pipeline, gate, passed=review.passed,
                    retry_count=issue.retry_count, retry_cap=self.t.retry_cap,
                    triggered_by_message=issue.triggered_by_message,
                )
                self._transition(
                    issue, outcome.state, gate_type=outcome.gate_type,
                    event_type=outcome.event_type,
                    payload={"reasons": review.reasons},
                    retry_count=outcome.retry_count,
                )
                if outcome.state == IssueState.DONE.value:
                    self._release_agent(issue)
                    summary.completed += 1
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
                # A human directive is a fresh start: only judge what happened
                # after the latest one (events arrive newest-first).
                cut = next((i for i, e in enumerate(events)
                            if e.event_type == "directive"), None)
                if cut is not None:
                    events = events[:cut]
                signals = focus.mechanical_signals(issue, events, self.t)
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
        self._decompose(summary)
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
