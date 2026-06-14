"""Maintenance lane (v1 standing backlog): the pure backfill selector, the goal
kind in the repository, and the engine wiring (perpetual goal, idle-only assignment)."""

from __future__ import annotations

from orchestrator import repository as repo
from orchestrator.engine import focus
from orchestrator.models import GoalState, Issue, IssueState

from test_engine import _make_engine  # reuse the StubReasoner engine builder


def _issue(id_: int, goal_id: int, team: str = "backend",
           state: str = IssueState.READY.value) -> Issue:
    return Issue(id=id_, goal_id=goal_id, title=f"i{id_}", team=team, state=state)


# --- pure selector --------------------------------------------------------- #

def test_standard_first_then_eligible_maintenance():
    # standard work on frontend; maintenance on backend (idle) — both eligible.
    ready = [_issue(2, goal_id=9, team="backend"),       # 9 = maintenance goal
             _issue(1, goal_id=1, team="frontend")]      # standard, different team
    out = focus.select_assignable(ready, in_progress=[], maintenance_goal_ids={9})
    assert [i.id for i in out] == [1, 2]  # standard (#1) first, then maintenance (#2)


def test_maintenance_gated_when_team_has_standard_ready_work():
    ready = [_issue(1, goal_id=1, team="backend"),        # standard backend work
             _issue(2, goal_id=9, team="backend")]        # backend maintenance
    out = focus.select_assignable(ready, in_progress=[], maintenance_goal_ids={9})
    assert [i.id for i in out] == [1]  # maintenance withheld while standard queued


def test_maintenance_gated_when_team_has_standard_in_progress_work():
    ready = [_issue(2, goal_id=9, team="backend")]                 # only maintenance ready
    in_progress = [_issue(1, goal_id=1, team="backend")]           # standard already running
    out = focus.select_assignable(ready, in_progress, maintenance_goal_ids={9})
    assert out == []  # team busy with standard work → no backfill


def test_maintenance_runs_when_its_team_is_idle_even_if_another_is_busy():
    ready = [_issue(2, goal_id=9, team="backend")]                 # backend maintenance
    in_progress = [_issue(1, goal_id=1, team="frontend")]          # only frontend busy
    out = focus.select_assignable(ready, in_progress, maintenance_goal_ids={9})
    assert [i.id for i in out] == [2]  # per-team: backend idle → backend maintenance runs


# --- repository ------------------------------------------------------------ #

def test_create_maintenance_goal_and_ids(pool):
    g = repo.create_goal(pool, "Backend upkeep", kind="maintenance", state="active")
    assert g.kind == "maintenance"
    assert g.id in repo.maintenance_goal_ids(pool)
    std = repo.create_goal(pool, "Real feature")
    assert std.kind == "standard"
    assert std.id not in repo.maintenance_goal_ids(pool)


# --- engine wiring --------------------------------------------------------- #

def test_maintenance_goal_is_perpetual(settings, pool):
    """A maintenance goal whose tasks are all done is never auto-completed."""
    g = repo.create_goal(pool, "Upkeep", pipeline="pipeline-1",
                         kind="maintenance", state="active")
    issue = repo.create_issue(pool, g.id, "tidy", team="backend")
    repo.update_state(pool, issue.id, IssueState.DONE.value)
    engine = _make_engine(settings, pool)
    engine._reconcile(engine_summary := type("S", (), {"goals_done": 0, "goals_paused": 0})())
    assert repo.get_goal(pool, g.id).state == "active"          # still active, not done
    assert engine_summary.goals_done == 0


def test_standard_work_preempts_maintenance_for_idle_worker(settings, pool):
    """With one idle backend dev and both a standard and a maintenance task ready,
    the engine assigns the standard one and leaves maintenance unassigned."""
    repo.register_agent(pool, "backend", "dev")
    std = repo.create_goal(pool, "Feature", pipeline="pull-1")
    si = repo.create_issue(pool, std.id, "build feature", team="backend", pipeline="pull-1")
    maint = repo.create_goal(pool, "Upkeep", pipeline="pull-1",
                             kind="maintenance", state="active")
    mi = repo.create_issue(pool, maint.id, "chore", team="backend", pipeline="pull-1")
    # mark both READY directly (skip the planning leg for a deterministic check)
    repo.update_state(pool, si.id, IssueState.READY.value)
    repo.update_state(pool, mi.id, IssueState.READY.value)

    engine = _make_engine(settings, pool)
    engine._assign(type("S", (), {"assigned": 0, "completed": 0, "errors": []})())

    assert repo.get_issue(pool, si.id).state == IssueState.IN_PROGRESS.value
    assert repo.get_issue(pool, mi.id).state == IssueState.READY.value  # withheld
