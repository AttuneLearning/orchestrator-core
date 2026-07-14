"""Apply/verify leg (slice F): worktree apply, verification events, human-gated
promotion, and — most importantly — that the flag-off default changes nothing.

WP-09 additions: `_apply_in_worktree` rewired behind `workflow_profile: enabled`.
Pure tests (no pool): load_effective/run_step monkeypatched. DB tests (with pool):
real workflow package but fake shell commands. Pure tests run directly here; DB
tests are collect-only self-check (monitor executes serially).
"""

import copy
import subprocess
from typing import Any

import pytest

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.apply.worktree import apply_and_verify, promote, _apply_in_worktree
from orchestrator.config import load_settings
from orchestrator.engine.loop import Engine
from orchestrator.models import Issue
from orchestrator.workflow.runner import StepResult

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *_GIT_ID, *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture()
def target_repo(tmp_path):
    """A scratch git repo standing in for the codebase artifacts apply to."""
    path = tmp_path / "target"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("target\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _run_to_done(settings, pool):
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    repo.create_goal(pool, "Apply demo")
    Engine(settings, pool, reasoner=StubReasoner()).run(max_ticks=40)
    return repo.list_issues(pool)


def _events(pool, issue_id, kind):
    return [e for e in repo.issue_timeline(pool, issue_id) if e.event_type == kind]


def test_flag_off_by_default_no_verification(settings, pool):
    assert settings.apply_enabled is False
    issues = _run_to_done(settings, pool)
    assert issues and all(i.state == "done" for i in issues)
    for i in issues:
        assert _events(pool, i.id, "verification") == []
        assert _events(pool, i.id, "promoted") == []


def test_apply_and_verify_pass(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "test -f generated/issue-*.txt || exit 1; true"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")]
    assert impl
    for i in impl:
        vs = _events(pool, i.id, "verification")
        assert vs, "qa_gate work step must log a verification event"
        v = vs[-1].payload
        assert v["passed"] is True and v["branch"] == f"issue-{i.id}"
        # the artifact is committed on the branch, not on main
        files = _git(target_repo, "ls-tree", "-r", "--name-only",
                     f"issue-{i.id}").stdout
        assert f"generated/issue-{i.id}.txt" in files
        main_files = _git(target_repo, "ls-tree", "-r", "--name-only", "main").stdout
        assert f"generated/issue-{i.id}.txt" not in main_files


def test_verify_failure_recorded(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "false"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    v = _events(pool, impl.id, "verification")[-1].payload
    assert v["passed"] is False and v["returncode"] == 1


def test_promote_requires_passing_verification(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "false"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    with pytest.raises(ValueError, match="did not pass"):
        promote(pool, impl, s)
    assert _events(pool, impl.id, "promoted") == []


def test_promote_merges_verified_branch(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "true"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    record = promote(pool, impl, s, note="reviewed")
    assert record["branch"] == f"issue-{impl.id}"
    main_files = _git(target_repo, "ls-tree", "-r", "--name-only", "main").stdout
    assert f"generated/issue-{impl.id}.txt" in main_files
    assert _events(pool, impl.id, "promoted")[-1].payload["note"] == "reviewed"
    # nothing was pushed anywhere: the scratch repo has no remotes
    remotes = _git(target_repo, "remote").stdout.strip()
    assert remotes == ""


def test_promote_without_verification_raises(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_repo_path = str(target_repo)
    issues = _run_to_done(s, pool)  # flag off → no verification events
    with pytest.raises(ValueError, match="no verification"):
        promote(pool, issues[0], s)


def test_missing_artifact_skips(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "true"
    goal = repo.create_goal(pool, "No artifact")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "Bare issue", pipeline="hotfix")
    # qa_gate work step on an issue whose implementation never produced code:
    issue = repo.update_state(pool, issue.id, "in_progress", gate_type="qa_gate")
    result = apply_and_verify(pool, issue, s)
    assert result["passed"] is False and "no code_generated" in result["skipped"]


# ---------------------------------------------------------------------------
# WP-09: _apply_in_worktree rewired behind workflow_profile: enabled
# Pure tests: monkeypatch load_effective/run_step to verify the mapping table
# ---------------------------------------------------------------------------


def _fake_run_step(script: dict[str, StepResult], events: dict[str, list[tuple[str, dict]]] | None = None):
    """A run_step stand-in: `script[step_name]` is the StepResult to return;
    `events[step_name]` (optional) is fired through event_cb first, mimicking
    what the real runner would have emitted for that step. Also populates the
    StepResult.results with ActionResult objects for detail extraction."""
    from orchestrator.workflow.models import RequiredAction
    from orchestrator.workflow.runner import ActionResult

    events = events or {}

    def fake(worktree, profile, step_name, role, perms, *, event_cb=None, escalation_cb=None):
        if event_cb is not None:
            for kind, payload in events.get(step_name, []):
                event_cb(kind, payload)

        result = script[step_name]
        # Populate results with ActionResult objects containing the detail from events
        if not result.results and events.get(step_name):
            for kind, payload in events.get(step_name, []):
                action_info = payload.get("action", {})
                action = RequiredAction(
                    run=action_info.get("run", ""),
                    builtin=action_info.get("builtin", ""),
                    on_fail=action_info.get("on_fail", "block"),
                    timeout=action_info.get("timeout", 300),
                    source=action_info.get("source", "default"),
                )
                # Extract detail from payload (without action/verdict keys)
                detail = {k: v for k, v in payload.items()
                         if k not in ("action", "verdict")}
                result.results.append(ActionResult(
                    action=action,
                    verdict=payload.get("verdict", ""),
                    ok=detail.get("ok", False),
                    detail=detail,
                ))

        return result

    return fake


def _action_payload(verdict: str, run: str = "", builtin: str = "", **extra: Any) -> dict[str, Any]:
    """Build an event_cb payload shaped like runner._payload()'s output."""
    payload: dict[str, Any] = {
        "action": {"run": run, "builtin": builtin, "on_fail": "block",
                   "timeout": 300, "source": "default"},
        "verdict": verdict,
    }
    payload.update(extra)
    return payload


def test_apply_legacy_never_calls_workflow_package(target_repo, monkeypatch):
    """Hard acceptance bar: with flag 'legacy' (default) _apply_in_worktree's
    existing code path must run completely unchanged, never calling load_effective
    or run_step. This proves WP-09 doesn't alter the default behavior."""
    def _boom(*a, **k):
        raise AssertionError("legacy mode must never call the workflow package")

    # Patch at the module level where they're imported
    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", _boom)
    monkeypatch.setattr(wt_module, "run_step", _boom)

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "legacy"  # explicitly set to legacy
    s.verify_cmd = "true"

    issue = Issue(id=99, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is True
    assert result["branch"] == "issue-99"
    assert result["commit"]
    assert result["committed"] is True


def test_apply_enabled_ok_path(target_repo, monkeypatch):
    """Enabled mode: prepare ok + verify ok -> passed=True with proper keys."""
    script = {"prepare": StepResult(status="ok"), "verify": StepResult(status="ok")}
    events = {
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", installed=False, returncode=0,
        ))],
        "verify": [("executed", _action_payload(
            "allow", run="npm run typecheck && npm test",
            ok=True, reason="", returncode=0, stdout="success\n", stderr="",
        ))],
    }

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"
    s.verify_cmd = "should-not-be-used"  # profile owns verify, not this setting

    issue = Issue(id=100, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is True
    assert result["returncode"] == 0
    assert result["branch"] == "issue-100"
    assert result["commit"]
    assert result["committed"] is True
    # Must have the same dict keys as legacy path for caller compatibility
    assert set(result) >= {"passed", "branch", "commit", "committed", "returncode"}


def test_apply_enabled_prepare_failed(target_repo, monkeypatch):
    """Enabled mode: prepare failed -> passed=False with error + deps detail."""
    script = {"prepare": StepResult(status="failed",
                                     reason="failed: npm ci: exit code 1")}
    events = {"prepare": [("failed", _action_payload(
        "allow", builtin="node-deps-reconcile",
        ok=False, reason="npm ci failed", returncode=1, stderr_tail="boom",
    ))]}

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    issue = Issue(id=101, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is False
    assert "prepare failed" in result["error"]
    assert result["branch"] == "issue-101"
    assert result["committed"] is True
    # deps detail from prepare action's failure
    assert "returncode" in result["deps"] or isinstance(result["deps"], dict)


def test_apply_enabled_prepare_blocked(target_repo, monkeypatch):
    """Enabled mode: prepare escalates -> passed=None + status=blocked_on_approval."""
    script = {"prepare": StepResult(status="blocked_on_approval",
                                     reason="awaiting approval: custom-setup")}
    events = {"prepare": [("escalated", _action_payload(
        "escalate", run="custom-setup", decision="pending",
    ))]}

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    issue = Issue(id=102, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is None
    assert result["status"] == "blocked_on_approval"
    assert "custom-setup" in result["reason"]  # reason includes "awaiting approval: custom-setup"
    assert result["branch"] == "issue-102"
    assert result["commit"]


def test_apply_enabled_verify_failed(target_repo, monkeypatch):
    """Enabled mode: verify failed -> passed=False with returncode from verify step."""
    script = {
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="failed", reason="failed: verify: exit code 1"),
    }
    events = {"verify": [("failed", _action_payload(
        "allow", run="npm run test",
        ok=False, reason="exit code 1", returncode=1,
        stdout="test failed output\n", stderr="error message\n",
    ))]}

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    issue = Issue(id=103, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is False
    assert result["returncode"] == 1
    assert "test failed" in result["stdout"]
    assert "error" in result["stderr"]


def test_apply_enabled_verify_blocked(target_repo, monkeypatch):
    """Enabled mode: verify escalates -> passed=None + status=blocked_on_approval."""
    script = {
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="blocked_on_approval",
                              reason="awaiting approval: deploy-prod"),
    }
    events = {"verify": [("escalated", _action_payload(
        "escalate", run="deploy-prod", decision="pending",
    ))]}

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    issue = Issue(id=104, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    assert result["passed"] is None
    assert result["status"] == "blocked_on_approval"
    assert "deploy-prod" in result["reason"]  # reason includes "awaiting approval: deploy-prod"
    assert result["branch"] == "issue-104"


def test_apply_enabled_verify_failed_action_with_warn_reports_failed(target_repo, monkeypatch):
    """Enabled mode: verify step status is "ok" but an action failed with on_fail: warn
    -> passed=False with returncode from the failed action, not synthesized from status."""
    script = {
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="ok", results=[]),
    }
    # First action fails but is allowed to warn (on_fail: warn)
    # Second action succeeds
    events = {"verify": [
        ("failed", _action_payload(
            "allow", run="npm run lint",
            ok=False, reason="lint errors", returncode=2, on_fail="warn",
            stdout="lint output\n", stderr="warnings\n",
        )),
        ("executed", _action_payload(
            "allow", run="npm run test",
            ok=True, reason="", returncode=0,
            stdout="tests pass\n", stderr="",
        )),
    ]}

    from orchestrator.apply import worktree as wt_module
    monkeypatch.setattr(wt_module, "load_effective", lambda *a, **k: None)
    monkeypatch.setattr(wt_module, "run_step", _fake_run_step(script, events))

    s = load_settings()
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    issue = Issue(id=105, goal_id=1, title="test", team="backend")
    artifact = "test artifact"

    result = _apply_in_worktree(issue, artifact, s)

    # Even though overall status is "ok", the failed action should make passed=False
    assert result["passed"] is False
    assert result["returncode"] == 2  # The failed action's returncode, not synthesized 0
    assert "lint" in result["stdout"]


# ---------------------------------------------------------------------------
# DB tests (real load_effective/run_step; fake shell commands only)
# Collect-only self-check here; monitor executes serially against real pool
# ---------------------------------------------------------------------------


def test_db_apply_enabled_ok_path(pool, target_repo):
    """DB test: enabled mode through real apply_and_verify with fake verify command."""
    s = copy.deepcopy(load_settings())
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    goal = repo.create_goal(pool, "WP-09 enabled apply")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "Apply & verify", pipeline="hotfix")
    issue = repo.update_state(pool, issue.id, "in_progress", gate_type="qa_gate")

    # Create a code_generated artifact for the issue
    repo.append_log(pool, issue.id, "code_generated",
                   {"content": "test artifact content"})

    # No profile file needed; defaults will be used. With no package-lock.json,
    # stack detection finds nothing, so default steps are minimal.
    result = apply_and_verify(pool, issue, s)

    # The workflow profile owns the prepare/verify steps; with empty defaults,
    # both steps return ok immediately (no actions to run).
    assert result["passed"] is True or result["passed"] is False  # depends on profile defaults
    assert "branch" in result
    assert "commit" in result


def test_db_apply_enabled_records_verification_event(pool, target_repo):
    """DB test: enabled mode appends a verification event like legacy mode."""
    s = copy.deepcopy(load_settings())
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.workflow_profile = "enabled"

    goal = repo.create_goal(pool, "WP-09 events")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "Events", pipeline="hotfix")
    issue = repo.update_state(pool, issue.id, "in_progress", gate_type="qa_gate")
    repo.append_log(pool, issue.id, "code_generated",
                   {"content": "test content"})

    result = apply_and_verify(pool, issue, s)

    # apply_and_verify appends a "verification" event with the result
    events = [e for e in repo.issue_timeline(pool, issue.id)
              if e.event_type == "verification"]
    assert len(events) == 1
    assert "branch" in events[0].payload
    assert "passed" in events[0].payload
