"""WP-13: `blocked_on_approval` end-to-end semantics.

Exercises the REAL `verify_run` MCP tool (`orchestrator/mcp_server/tools_issues.py`)
and the REAL `_apply_in_worktree_enabled` (`orchestrator/apply/worktree.py`) against
a real Postgres pool (the `pool` fixture) and a real tmp git checkout, with the
real `escalation.make_escalation_cb` (WP-12) wired all the way through `run_step`
(WP-07/permissions WP-03) — this is the first test file to prove the FULL
escalate -> `pending_actions` row + comms message -> approve/deny -> resume loop
end to end, rather than a single component in isolation.

Per FANOUT-CLAUDE.md this file is written by the WP agent but NEVER executed by
it — self-check with `pytest --collect-only` only; the monitor runs it for real,
serially, at a wave/gate boundary.

Scenarios (plan §2.2 / WORKFLOW-PROFILE-TASKS.md WP-13):
  (a) first call blocks: a `pending_actions` row and an orchestration message
      exist; no `tests_run` event was appended; the issue's state/retry
      counters are untouched (a pending approval is "not attempted", not a
      failure).
  (b) approve, then the next `verify_run` call consumes the approval, actually
      executes the action (observed via a marker-file side effect), and verify
      completes normally.
  (c) deny, then the next `verify_run` call returns a REAL failure naming the
      denial — a denied action is a hard stop, not a re-pend.
  (d) the same escalate -> approve -> execute flow through the apply path
      (`_apply_in_worktree_enabled`), exercised directly (cheap: skips the
      full `apply_and_verify`/worktree-creation plumbing, which is unrelated
      to the escalation wiring under test here).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import repository as repo
from orchestrator.config import load_settings
from orchestrator.mcp_server import tools_issues

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(d), *_GIT_ID, *args], check=True, capture_output=True, text=True)


class _Recorder:
    """A minimal FastMCP stand-in (mirrors tests/test_workflow_cutover.py's
    harness): `register()` just wants something with a `.tool()` decorator."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _issue_tools(pool, settings):
    rec = _Recorder()
    tools_issues.register(rec, pool, settings)
    return rec.tools


@pytest.fixture
def gitrepo(tmp_path: Path) -> Path:
    """A scratch git repo that doubles as BOTH the "source" repo and the verify
    worktree (settings.verify_worktrees points straight at it), same shape as
    tests/test_workflow_cutover.py's `gitrepo` fixture."""
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    (d / "base.txt").write_text("base\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


def _settings(repo_path: Path, workspace_manifest: str = "") -> Any:
    s = load_settings()
    s.verify_worktrees = {"backend": str(repo_path)}
    s.workflow_profile = "enabled"
    s.workspace_manifest = workspace_manifest
    return s


def _verification_issue(pool, team: str = "backend"):
    goal = repo.create_goal(pool, "wp13-blocked-on-approval", pipeline="pull-1")
    issue = repo.create_issue(
        pool, goal.id, "blocked_on_approval e2e",
        "exercises the real escalate/approve/deny loop end to end",
        team=team, pipeline="pull-1",
    )
    repo.update_state(pool, issue.id, "in_progress", gate_type="verification")
    return issue


def _checkout_issue_branch(gitrepo: Path, issue_id: int) -> None:
    _git(gitrepo, "checkout", "-b", f"issue-{issue_id}", "main")


def _write_and_commit_profile(gitrepo: Path, yaml_text: str) -> None:
    """`.orchestrator/workflow.yaml` must be COMMITTED on the issue branch —
    verify_run's `git reset --hard && git clean -fd` (before checking out
    `_verify-<id>`) wipes any untracked file, same reasoning as
    tests/test_workflow_cutover.py's `_write_and_commit_repo_profile`."""
    d = gitrepo / ".orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(yaml_text)
    _git(gitrepo, "add", ".orchestrator/workflow.yaml")
    _git(gitrepo, "commit", "-qm", "test: add workflow profile")


def _pending_for(pool, issue_id: int, status: str = "pending") -> list[dict[str, Any]]:
    return [r for r in repo.list_pending_actions(pool, status=status) if r["issue_id"] == issue_id]


def _tests_run_events(pool, issue_id: int) -> list[Any]:
    return [e for e in repo.recent_events(pool, issue_id, limit=50) if e.event_type == "tests_run"]


# ---------------------------------------------------------------------------
# (a) first call blocks: pending row + message exist, no tests_run, issue
# state/retry counters untouched.


def test_blocked_first_call_creates_pending_row_and_message_no_state_change(gitrepo, pool):
    issue = _verification_issue(pool)
    _checkout_issue_branch(gitrepo, issue.id)
    _write_and_commit_profile(
        gitrepo,
        'prepare:\n  - run: "touch prepare-marker.txt"\n    on_fail: block\n',
    )
    before = repo.get_issue(pool, issue.id)

    tools = _issue_tools(pool, _settings(gitrepo))

    out = tools["verify_run"](issue.id)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert out["step"] == "prepare"
    assert out["action"] == "touch prepare-marker.txt"
    assert out["phase"] == "authorize"
    assert out.get("note")

    rows = _pending_for(pool, issue.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["step"] == "prepare"
    assert row["action"] == "touch prepare-marker.txt"
    assert row["action_kind"] == "run"
    assert "verify_run:backend" in row["requested_by"]
    assert "phase:authorize" in row["requested_by"]

    msgs = [m for m in repo.pending_messages(pool, to_team="orchestration") if m["issue_id"] == issue.id]
    assert len(msgs) == 1
    assert msgs[0]["priority"] == "high"

    # Not-attempted invariant: no failed tests_run event, no state/retry change.
    assert _tests_run_events(pool, issue.id) == []
    events = repo.recent_events(pool, issue.id, limit=20)
    escalated = [e for e in events if e.event_type == "action_escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload["machine"] is True
    assert escalated[0].payload["step"] == "prepare"
    assert escalated[0].payload["phase"] == "authorize"

    after = repo.get_issue(pool, issue.id)
    assert after.state == before.state
    assert after.retry_count == before.retry_count
    assert not (gitrepo / "prepare-marker.txt").exists()  # the action never actually ran

    # A second blocked verify_run call (re-poll while still pending) must hit
    # the dedup path in escalation.handle_escalation: no new pending_actions
    # row, no second action_escalated event — the audit already has the fact —
    # but the return payload still tells the worker it's blocked.
    second = tools["verify_run"](issue.id)
    assert second["passed"] is None
    assert second["status"] == "blocked_on_approval"
    assert second["step"] == "prepare"
    rows_after = _pending_for(pool, issue.id)
    assert len(rows_after) == 1
    assert rows_after[0]["id"] == row["id"]
    escalated_after = [e for e in repo.recent_events(pool, issue.id, limit=20)
                       if e.event_type == "action_escalated"]
    assert len(escalated_after) == 1


# ---------------------------------------------------------------------------
# (b) approve -> next verify_run call executes the action for real and
# completes verify.


def test_approve_then_second_call_executes_action_and_completes_verify(gitrepo, pool):
    issue = _verification_issue(pool)
    _checkout_issue_branch(gitrepo, issue.id)
    _write_and_commit_profile(
        gitrepo,
        'prepare:\n'
        '  - run: "touch execution-marker.txt"\n'
        '    on_fail: block\n'
        'verify:\n'
        '  - run: "echo profile-verify-ok"\n'
        '    on_fail: block\n',
    )
    # Grant the verify action (exact-match allow) but NOT the prepare action,
    # so prepare escalates while verify would authorize cleanly once reached.
    manifest = gitrepo.parent / "workspace.yaml"
    manifest.write_text('permissions:\n  allow:\n    - "echo profile-verify-ok"\n')

    tools = _issue_tools(pool, _settings(gitrepo, workspace_manifest=str(manifest)))

    first = tools["verify_run"](issue.id)
    assert first["passed"] is None
    assert first["status"] == "blocked_on_approval"
    assert first["step"] == "prepare"
    assert not (gitrepo / "execution-marker.txt").exists()

    pending = _pending_for(pool, issue.id)
    assert len(pending) == 1
    repo.resolve_pending_action(pool, pending[0]["id"], "approved", resolved_by="alice")

    second = tools["verify_run"](issue.id)

    assert second["passed"] is True
    assert second["returncode"] == 0
    assert (gitrepo / "execution-marker.txt").exists()  # the approved action really ran

    executed = [r for r in repo.list_pending_actions(pool, status="executed") if r["issue_id"] == issue.id]
    assert len(executed) == 1
    assert executed[0]["id"] == pending[0]["id"]  # the SAME row, one-shot consumed

    tests_run = _tests_run_events(pool, issue.id)
    assert len(tests_run) == 1
    assert tests_run[0].payload["returncode"] == 0
    assert tests_run[0].payload["cmd"] == "echo profile-verify-ok"


# ---------------------------------------------------------------------------
# (c) deny -> next verify_run call returns a REAL failure naming the denial.


def test_deny_then_second_call_returns_hard_failure_naming_denial(gitrepo, pool):
    issue = _verification_issue(pool)
    _checkout_issue_branch(gitrepo, issue.id)
    _write_and_commit_profile(
        gitrepo,
        'prepare:\n  - run: "some-denied-setup-step"\n    on_fail: block\n',
    )

    tools = _issue_tools(pool, _settings(gitrepo))

    first = tools["verify_run"](issue.id)
    assert first["status"] == "blocked_on_approval"

    pending = _pending_for(pool, issue.id)
    assert len(pending) == 1
    repo.resolve_pending_action(pool, pending[0]["id"], "denied", resolved_by="bob")

    second = tools["verify_run"](issue.id)

    assert second["passed"] is False
    assert "denied" in second["tail"].lower()

    # A denial is a hard stop: no new pending row, the denied row stays denied.
    assert _pending_for(pool, issue.id, status="pending") == []
    denied_rows = _pending_for(pool, issue.id, status="denied")
    assert len(denied_rows) == 1
    assert denied_rows[0]["id"] == pending[0]["id"]

    tests_run = _tests_run_events(pool, issue.id)
    assert len(tests_run) == 1
    assert tests_run[0].payload["returncode"] != 0


# ---------------------------------------------------------------------------
# (d) the same escalate -> approve -> execute flow through the apply path.


def test_apply_path_escalate_then_approve_executes_action(gitrepo, pool):
    """Cheap same-flow check directly against `_apply_in_worktree_enabled`
    (skips `apply_and_verify`'s worktree-creation/artifact-commit plumbing,
    which is unrelated to the escalation wiring under test) — proves `pool`
    threads through correctly and the approve/consume loop works on this call
    site too, not just verify_run's."""
    from orchestrator.apply.worktree import _apply_in_worktree_enabled
    from orchestrator.models import Issue

    issue_row = _verification_issue(pool)
    issue = Issue(id=issue_row.id, goal_id=issue_row.goal_id, title="t", team="backend")

    _checkout_issue_branch(gitrepo, issue.id)
    _write_and_commit_profile(
        gitrepo,
        'prepare:\n  - run: "touch apply-marker.txt"\n    on_fail: block\n'
    )
    settings = _settings(gitrepo)

    first = _apply_in_worktree_enabled(issue, gitrepo, settings, sha="deadbeef",
                                       committed=True, pool=pool)
    assert first["passed"] is None
    assert first["status"] == "blocked_on_approval"
    assert first["step"] == "prepare"
    assert first["action"] == "touch apply-marker.txt"
    assert first["phase"] == "authorize"
    assert not (gitrepo / "apply-marker.txt").exists()

    pending = _pending_for(pool, issue.id)
    assert len(pending) == 1
    assert pending[0]["step"] == "prepare"
    repo.resolve_pending_action(pool, pending[0]["id"], "approved", resolved_by="carol")

    second = _apply_in_worktree_enabled(issue, gitrepo, settings, sha="deadbeef",
                                        committed=True, pool=pool)

    assert (gitrepo / "apply-marker.txt").exists()  # the approved action really ran
    executed = [r for r in repo.list_pending_actions(pool, status="executed") if r["issue_id"] == issue.id]
    assert len(executed) == 1


def test_apply_path_without_pool_stays_pending_only(gitrepo):
    """`pool=None` (the default — every pre-WP-13 caller of `_apply_in_worktree`
    that never learned about `pool`) must stay source-compatible: no DB, so
    escalation_cb falls back to the runner's own "always pending" default —
    never a crash, never a silent execute."""
    from orchestrator.apply.worktree import _apply_in_worktree_enabled
    from orchestrator.models import Issue

    issue = Issue(id=999999, goal_id=1, title="t", team="backend")
    _checkout_issue_branch(gitrepo, issue.id)
    _write_and_commit_profile(
        gitrepo,
        'prepare:\n  - run: "touch no-pool-marker.txt"\n    on_fail: block\n'
    )
    settings = _settings(gitrepo)

    result = _apply_in_worktree_enabled(issue, gitrepo, settings, sha="deadbeef", committed=True)

    assert result["passed"] is None
    assert result["status"] == "blocked_on_approval"
    assert result["step"] == "prepare"
    assert not (gitrepo / "no-pool-marker.txt").exists()
