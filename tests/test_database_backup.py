import copy
import json

from orchestrator import repository as repo
from orchestrator.backup import backup_database
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine
from orchestrator.models import IssueState


def _settings_with_backup(settings, tmp_path):
    s = copy.deepcopy(settings)
    s.database_backup_enabled = True
    s.database_backup_dir = str(tmp_path)
    return s


def _settings_with_e2e_pipeline(settings, tmp_path):
    s = _settings_with_backup(settings, tmp_path)
    gates = s.pipelines["pipelines"]["pull-fe"]["gates"]
    gates.insert(4, {
        "type": "e2e",
        "order": 5,
        "owner": "qa",
        "mode": "verdict",
        "on_failure": "implementation",
        "description": "Injected test e2e gate.",
    })
    for index, gate in enumerate(gates, start=1):
        gate["order"] = index
    return s


def test_backup_database_writes_file(settings, pool, tmp_path):
    s = _settings_with_backup(settings, tmp_path)
    repo.create_goal(pool, "backup me")

    result = backup_database(s, reason="unit-test")

    assert result["passed"] is True
    path = tmp_path / result["path"].rsplit("/", 1)[-1]
    assert path.exists()
    assert result["bytes"] > 0


def test_e2e_gate_success_records_database_backup(settings, pool, tmp_path):
    s = _settings_with_e2e_pipeline(settings, tmp_path)
    goal = repo.create_goal(pool, "frontend e2e", pipeline="pull-fe", state="active")
    issue = repo.create_issue(
        pool,
        goal.id,
        "run e2e",
        team="frontend",
        pipeline="pull-fe",
    )
    issue = repo.update_state(pool, issue.id, IssueState.IN_REVIEW.value, gate_type="e2e")

    Engine(s, pool, reasoner=StubReasoner()).tick()

    events = repo.recent_events(pool, issue.id, limit=20)
    backup_events = [e for e in events if e.event_type == "database_backup"]
    assert backup_events
    assert backup_events[0].payload["passed"] is True
    assert backup_events[0].payload["issue_id"] == issue.id


def test_goal_completion_records_database_backup(settings, pool, tmp_path):
    s = _settings_with_backup(settings, tmp_path)
    goal = repo.create_goal(pool, "complete with backup", state="active")
    issue = repo.create_issue(pool, goal.id, "done")
    repo.update_state(pool, issue.id, IssueState.DONE.value)

    Engine(s, pool, reasoner=StubReasoner()).tick()

    last = repo.get_system_state(pool, "last_database_backup")
    assert last is not None
    payload = json.loads(last)
    assert payload["passed"] is True
    assert payload["goal_id"] == goal.id
