"""WP-08: `verify_run` rewired behind `workflow_profile: enabled`.

Pure tests (run directly, no Postgres): every DB touch point `verify_run` uses
(`repo.get_issue`, `repo.append_log`, `_agent_stamp`) is monkeypatched to an
in-memory recorder, so these exercise the FULL tool body — including the real
git checkout against a tmp repo — without a `pool` fixture. `load_effective`/
`run_step` are monkeypatched only in the `enabled`-mode tests, to pin down the
StepResult -> return-value mapping table from the WP spec; the `legacy`-mode
tests monkeypatch them to raise, proving the legacy path never calls them.

A DB-backed pair of tests at the bottom exercises the same two flag values
through real `repository.py` writes (real `load_effective`/`run_step`, fake
shell commands only) — collect-only self-check here; the monitor executes
them serially against the real pool.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import repository as repo
from orchestrator.config import load_settings
from orchestrator.mcp_server import tools_issues
from orchestrator.models import Issue
from orchestrator.workflow.models import RequiredAction
from orchestrator.workflow.runner import StepResult, ActionResult

# ---------------------------------------------------------------------------
# Shared harness (mirrors tests/test_commit_guardrails.py's _Recorder/
# _issue_tools; the `db` fixture additionally fakes every repository.py call
# verify_run makes, so the pure tests need no pool/Postgres at all).


class _Recorder:
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


def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def gitrepo(tmp_path: Path) -> Path:
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "base.txt").write_text("base\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    _git(d, "checkout", "-q", "-B", "issue-1", "main")
    return d


def _settings(repo_path: Path, workflow_profile: str = "legacy") -> Any:
    s = load_settings()
    s.verify_worktrees = {"backend": str(repo_path)}
    s.workflow_profile = workflow_profile
    return s


class _Events:
    """Records every `repo.append_log(pool, issue_id, event_type, payload)` call
    verify_run makes, keyed the same way `repository.append_log` is called."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict]] = []

    def append_log(self, pool, issue_id, event_type, payload=None) -> None:
        self.calls.append((issue_id, event_type, payload or {}))

    def of(self, event_type: str) -> list[dict]:
        return [p for (_, et, p) in self.calls if et == event_type]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, gitrepo: Path) -> _Events:
    """Patch every DB touch point verify_run uses with in-memory fakes, so the
    tool body runs for real (git checkout included) without Postgres."""
    issue = Issue(id=1, goal_id=1, title="t", team="backend", gate_type="verification")
    events = _Events()
    monkeypatch.setattr(tools_issues.repo, "get_issue", lambda pool, issue_id: issue)
    monkeypatch.setattr(tools_issues.repo, "append_log", events.append_log)
    monkeypatch.setattr(tools_issues, "_agent_stamp", lambda pool, issue: {})
    return events


def _fake_run_step(script: dict[str, StepResult], events: dict[str, list[tuple[str, dict]]] | None = None):
    """A run_step stand-in: `script[step_name]` is the StepResult to return;
    `events[step_name]` (optional) is fired through event_cb first, mimicking
    what the real runner would have emitted for that step."""
    events = events or {}

    def fake(worktree, profile, step_name, role, perms, *, event_cb=None, escalation_cb=None):
        if event_cb is not None:
            for kind, payload in events.get(step_name, []):
                event_cb(kind, payload)
        return script[step_name]

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


# ---------------------------------------------------------------------------
# legacy mode: byte-identical behavior + workflow package never invoked


def test_legacy_path_never_calls_workflow_package(gitrepo, db, monkeypatch):
    """Hard acceptance bar: with the flag unset (default 'legacy') verify_run's
    existing code path must run completely unchanged, and must not even call
    load_effective/run_step."""
    def _boom(*a, **k):
        raise AssertionError("legacy mode must never call the workflow package")
    monkeypatch.setattr(tools_issues, "load_effective", _boom)
    monkeypatch.setattr(tools_issues, "run_step", _boom)

    s = _settings(gitrepo, workflow_profile="legacy")
    s.verify_cmd = "exit 0"
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert out["returncode"] == 0
    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert tests_run[0]["machine"] is True
    assert tests_run[0]["cmd"] == "exit 0"
    assert tests_run[0]["returncode"] == 0
    assert db.of("deps_reinstalled") == []  # no package-lock.json in this fixture


def test_legacy_red_path_records_failure(gitrepo, db):
    s = _settings(gitrepo, workflow_profile="legacy")
    s.verify_cmd = "exit 1"
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert out["returncode"] == 1
    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert tests_run[0]["returncode"] == 1


# ---------------------------------------------------------------------------
# enabled mode: StepResult -> return-value mapping table


def test_enabled_ok_path_emits_deps_reinstalled_and_tests_run(gitrepo, db, monkeypatch):
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="ok"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="npm ci ran", checked=True, installed=True, returncode=0,
        ))],
        "verify": [("executed", _action_payload(
            "allow", run="npm run typecheck && npm test",
            ok=True, reason="", returncode=0, stdout="all good\n", stderr="",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    s.verify_cmd = "should-not-be-used"  # enabled mode: profile owns verify, not settings.verify_cmd
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert out["returncode"] == 0

    deps_reinstalled = db.of("deps_reinstalled")
    assert len(deps_reinstalled) == 1
    assert deps_reinstalled[0]["machine"] is True
    assert deps_reinstalled[0]["installed"] is True

    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    tr = tests_run[0]
    assert tr["machine"] is True
    assert tr["cmd"] == "npm run typecheck && npm test"  # from the profile action, not verify_cmd
    assert tr["returncode"] == 0
    # Same payload keys the legacy path emits for tests_run.
    assert set(tr) >= {"machine", "cmd", "returncode", "duration_s", "branch",
                       "stdout_tail", "stderr_tail", "deps"}


def test_enabled_prepare_failed_records_tests_run_failure(gitrepo, db, monkeypatch):
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="failed",
                             reason="failed: node-deps-reconcile: npm ci failed"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("failed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=False, reason="npm ci failed", returncode=1, stderr_tail="boom",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert "dependency install failed before verify" in out["tail"]
    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert tests_run[0]["returncode"] == 1
    assert tests_run[0]["deps"]["reason"] == "npm ci failed"
    assert db.of("deps_reinstalled") == []  # verify step never reached


def test_enabled_prepare_blocked_never_records_failed_tests_run(gitrepo, db, monkeypatch):
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="blocked_on_approval",
                             reason="awaiting approval: rm -rf /tmp/x"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("escalated", _action_payload(
            "escalate", run="rm -rf /tmp/x", decision="pending",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert out["step"] == "prepare"
    assert out["action"] == "rm -rf /tmp/x"
    assert out["phase"] == "authorize"
    assert "note" in out
    assert db.of("tests_run") == []  # never a failed tests_run event on a block
    # `action_escalated` is emitted by repository.create_pending_action (owned by
    # escalation.handle_escalation), not by this tool — this pure test's fake
    # run_step never reaches real persistence, so there's no event to observe
    # here. Coverage for the event itself lives in the DB-backed tests below
    # (test_db_enabled_prepare_blocked_records_no_failed_tests_run).


def test_enabled_verify_failed_records_tests_run_failure(gitrepo, db, monkeypatch):
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="failed", reason="failed: npm test: exit code 1"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", returncode=0,
        ))],
        "verify": [("failed", _action_payload(
            "allow", run="npm run typecheck && npm test",
            ok=False, reason="exit code 1", returncode=1,
            stdout="fail output", stderr="err output",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert out["returncode"] == 1
    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert tests_run[0]["returncode"] == 1
    assert "fail output" in tests_run[0]["stdout_tail"]
    assert "err output" in tests_run[0]["stderr_tail"]


def test_enabled_verify_blocked_never_records_failed_tests_run(gitrepo, db, monkeypatch):
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="blocked_on_approval",
                             reason="awaiting approval: custom-deploy-step"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", returncode=0,
        ))],
        "verify": [("escalated", _action_payload(
            "escalate", run="custom-deploy-step", decision="pending",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert out["step"] == "verify"
    assert out["action"] == "custom-deploy-step"
    assert out["phase"] == "authorize"
    assert "note" in out
    assert db.of("tests_run") == []
    # `action_escalated` is emitted by repository.create_pending_action, not by
    # this tool — see the matching comment in
    # test_enabled_prepare_blocked_never_records_failed_tests_run above; DB
    # coverage lives in the DB-backed tests below.


def test_enabled_verify_failed_action_with_warn_reports_failed(gitrepo, db, monkeypatch):
    """When verify step status is 'ok' but an action failed with on_fail: warn,
    the result should still report passed=False with the action's returncode."""
    # Create an action that fails but is allowed to warn
    failed_action = RequiredAction(run="npm test", on_fail="warn")
    failed_result = ActionResult(
        action=failed_action,
        verdict="allow",
        ok=False,  # The action failed
        detail={"returncode": 42, "stdout": "test output", "stderr": "errors"}
    )
    # Subsequent action that succeeded
    good_action = RequiredAction(run="echo ok", on_fail="block")
    good_result = ActionResult(
        action=good_action,
        verdict="allow",
        ok=True,
        detail={"returncode": 0, "stdout": "ok", "stderr": ""}
    )

    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="ok", results=[failed_result, good_result]),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", returncode=0,
        ))],
        "verify": [
            ("failed", _action_payload(
                "allow", run="npm test",
                ok=False, reason="tests failed", returncode=42, stdout="test output", stderr="errors",
            )),
            ("executed", _action_payload(
                "allow", run="echo ok",
                ok=True, reason="", returncode=0, stdout="ok", stderr="",
            )),
        ],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    # Even though overall status is "ok", the failed action should make passed=False
    assert out["passed"] is False
    assert out["returncode"] == 42  # The failed action's returncode

    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert tests_run[0]["returncode"] == 42
    assert "test output" in tests_run[0]["stdout_tail"]


# ---------------------------------------------------------------------------
# DB-backed tests (real repository.py writes; real load_effective/run_step,
# fake shell commands only) — collect-only self-check; monitor executes.


def _real_verification_issue(pool, team: str = "backend"):
    goal = repo.create_goal(pool, "wp08-cutover", pipeline="pull-1")
    issue = repo.create_issue(
        pool, goal.id, "profile-driven verify_run",
        "exercises workflow_profile: enabled through real repository.py writes",
        team=team, pipeline="pull-1",
    )
    repo.update_state(pool, issue.id, "in_progress", gate_type="verification")
    return issue


def _write_repo_profile(gitrepo: Path, yaml_text: str) -> None:
    """Write .orchestrator/workflow.yaml as an UNTRACKED file (legacy behavior).

    WARNING: This is used by pure tests only. For DB tests (which call verify_run),
    use _write_and_commit_repo_profile instead — untracked files don't survive
    verify_run's `git reset --hard && git clean -fd` cleanup step.
    """
    d = gitrepo / ".orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(yaml_text)


def _write_and_commit_repo_profile(gitrepo: Path, yaml_text: str, branch: str = None) -> None:
    """Write and commit .orchestrator/workflow.yaml to the git repository.

    Untracked files don't survive verify_run's `git reset --hard && git clean -fd`
    cleanup step (line 654-655 of tools_issues.py), so the profile must be COMMITTED
    on the issue branch for verify_run to load it after checkout. This ensures the
    repo-profile travels WITH the code, matching the designed contract that profiles
    are artifacts of the codebase, not transient test setup.
    """
    d = gitrepo / ".orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(yaml_text)
    _git(gitrepo, "add", ".orchestrator/workflow.yaml")
    _git(gitrepo, "commit", "-qm", "test: add workflow profile")


def test_db_enabled_ok_path_records_tests_run(gitrepo, pool):
    issue = _real_verification_issue(pool)
    _git(gitrepo, "branch", "-m", "issue-1", f"issue-{issue.id}")
    # No package-lock.json -> stack "" -> no adapter defaults; the repo-layer
    # profile below is the only source for verify (prepare stays empty/ok).
    # Use a distinctive fake command to prove the profile is being read, not defaults.
    _write_and_commit_repo_profile(gitrepo, 'verify:\n  - run: "echo profile-verify-ok"\n    on_fail: block\n')
    manifest = gitrepo.parent / "workspace.yaml"
    manifest.write_text("permissions:\n  bypass: true\n")  # authorize the custom `run` action

    s = _settings(gitrepo, workflow_profile="enabled")
    s.workspace_manifest = str(manifest)
    tools = _issue_tools(pool, s)

    out = tools["verify_run"](issue.id)

    assert out["passed"] is True
    assert out["returncode"] == 0
    ev = [e for e in repo.recent_events(pool, issue.id, limit=20) if e.event_type == "tests_run"]
    assert len(ev) == 1
    assert ev[0].payload["machine"] is True
    assert ev[0].payload["returncode"] == 0
    # Prove the cmd comes from the committed profile, not from defaults or settings.
    assert ev[0].payload["cmd"] == "echo profile-verify-ok"


def test_db_enabled_prepare_blocked_records_no_failed_tests_run(gitrepo, pool):
    issue = _real_verification_issue(pool)
    _git(gitrepo, "branch", "-m", "issue-1", f"issue-{issue.id}")
    # A custom (non-builtin) prepare action with no granted permission escalates
    # rather than running — no workspace manifest configured here.
    _write_and_commit_repo_profile(gitrepo, 'prepare:\n  - run: "custom-unapproved-step"\n    on_fail: block\n')

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(pool, s)

    out = tools["verify_run"](issue.id)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert out["step"] == "prepare"
    assert out["action"] == "custom-unapproved-step"
    assert out["phase"] == "authorize"
    assert "note" in out
    kinds = [e.event_type for e in repo.recent_events(pool, issue.id, limit=20)]
    assert "tests_run" not in kinds
    assert "action_escalated" in kinds
    # Prove the escalation is recorded with the right step and action.
    escalated = [e for e in repo.recent_events(pool, issue.id, limit=20) if e.event_type == "action_escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload["step"] == "prepare"
    assert escalated[0].payload["action"] == "custom-unapproved-step"


# ---------------------------------------------------------------------------
# cleanup step wiring (WP-19): enabled mode routes through run_step,
# legacy mode unchanged; cleanup happens before checkout; denied actions
# carry cmd in tests_run event


def test_legacy_cleanup_never_calls_run_step(gitrepo, db, monkeypatch):
    """Hard acceptance bar: with the flag 'legacy', cleanup must use inline
    reset/clean and must not call run_step."""
    def _boom(*a, **k):
        raise AssertionError("legacy mode must never call run_step")
    monkeypatch.setattr(tools_issues, "run_step", _boom)

    s = _settings(gitrepo, workflow_profile="legacy")
    s.verify_cmd = "exit 0"
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert out["returncode"] == 0


def test_enabled_cleanup_failure_returns_failed(gitrepo, db, monkeypatch):
    """Cleanup failure (on_fail: block by default) prevents checkout and verify."""
    cleanup_script = {"cleanup": StepResult(status="failed",
                                            reason="failed: git reset: permission denied")}
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(cleanup_script))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert "cleanup failed" in out["tail"]
    assert "permission denied" in out["tail"]


def test_enabled_cleanup_blocked_on_approval_returns_blocked(gitrepo, db, monkeypatch):
    """Cleanup escalation (e.g. custom cleanup action) blocks without proceeding to checkout."""
    cleanup_script = {"cleanup": StepResult(status="blocked_on_approval",
                                            reason="awaiting approval: custom-cleanup-hook")}
    cleanup_events = {
        "cleanup": [("escalated", _action_payload(
            "escalate", run="custom-cleanup-hook", decision="pending",
        ))]
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(cleanup_script, cleanup_events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert "cleanup blocked on approval" in out["tail"]


# ---------------------------------------------------------------------------
# refresh step wiring: runs after cleanup, BEFORE the deterministic
# `git checkout -B verify_branch branch` — a failed/blocked refresh must not
# check out a stale worktree. An empty refresh (no actions, the default
# profile) is a no-op that proceeds straight to checkout.


def _verify_branch_exists(gitrepo) -> bool:
    return subprocess.run(
        ["git", "-C", str(gitrepo), "rev-parse", "--verify", "--quiet", "_verify-1"],
        capture_output=True,
    ).returncode == 0


def test_enabled_refresh_runs_before_checkout(gitrepo, db, monkeypatch):
    """refresh is called (and completes) before checkout — call-order proof
    via a run_step recorder, mirroring the services no-op call-order test."""
    calls: list[str] = []
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="ok"),
    }
    events = {
        "verify": [("executed", _action_payload(
            "allow", run="npm run typecheck && npm test",
            ok=True, reason="", returncode=0, stdout="ok\n", stderr="",
        ))],
    }
    base_fake = _fake_run_step(script, events)

    def _tracking_fake(worktree, profile, step_name, role, perms, **kw):
        calls.append(step_name)
        return base_fake(worktree, profile, step_name, role, perms, **kw)

    monkeypatch.setattr(tools_issues, "run_step", _tracking_fake)

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert calls == ["cleanup", "refresh", "prepare", "verify"]
    # checkout actually ran (refresh didn't short-circuit it)
    assert _verify_branch_exists(gitrepo)


def test_enabled_refresh_failed_returns_failure_before_checkout(gitrepo, db, monkeypatch):
    """A failed refresh returns a clear verify failure naming refresh, WITHOUT
    checking out — never verify a stale worktree."""
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="failed",
                             reason="failed: git merge main: exit code 1"),
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert "refresh failed" in out["tail"]
    assert "git merge main" in out["tail"]
    assert not _verify_branch_exists(gitrepo)


def test_enabled_refresh_blocked_returns_blocked_before_checkout(gitrepo, db, monkeypatch):
    """A blocked refresh returns blocked (passed None + status blocked_on_approval)
    without proceeding to checkout."""
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="blocked_on_approval",
                             reason="awaiting approval: git merge main"),
    }
    events = {
        "refresh": [("escalated", _action_payload(
            "escalate", run="git merge main", decision="pending",
        ))]
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is None
    assert out["status"] == "blocked_on_approval"
    assert "refresh blocked on approval" in out["tail"]
    assert not _verify_branch_exists(gitrepo)


def test_legacy_refresh_never_calls_run_step(gitrepo, db, monkeypatch):
    """Hard acceptance bar: with the flag 'legacy', refresh must never be
    invoked — run_step is not called for it (or anything else)."""
    def _boom(*a, **k):
        raise AssertionError("legacy mode must never call run_step (incl. refresh)")
    monkeypatch.setattr(tools_issues, "run_step", _boom)

    s = _settings(gitrepo, workflow_profile="legacy")
    s.verify_cmd = "exit 0"
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert out["returncode"] == 0


def test_enabled_verify_denied_action_includes_cmd_and_denial_reason(gitrepo, db, monkeypatch):
    """When a verify action is denied (verdict: deny, not escalate->denied),
    the tests_run event should include the cmd string and denial reason in stderr."""
    denied_action = RequiredAction(run="custom-unapproved-verify")
    denied_result = ActionResult(
        action=denied_action,
        verdict="deny",
        ok=False,
        detail={"reason": "denied: custom-unapproved-verify"}
    )

    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="failed", results=[denied_result],
                            reason="denied: custom-unapproved-verify"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", returncode=0,
        ))],
        "verify": [("refused", _action_payload(
            "deny", run="custom-unapproved-verify",
            reason="denied: custom-unapproved-verify",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert out["returncode"] == 1

    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    tr = tests_run[0]
    # WP-19 NIT: denied action should have cmd set (not empty string)
    assert tr["cmd"] == "custom-unapproved-verify"
    # WP-19 NIT: denial reason should be in stderr_tail so the event is self-naming
    assert "denied" in tr["stderr_tail"].lower() or "denied" in out["tail"].lower()


# ---------------------------------------------------------------------------
# services step wiring (WP-18 Gate D / plan step-4 acceptance): the services
# step runs BEFORE prepare and gates the build; a down service fails FAST
# with a clear message instead of blocking on approval; no services declared
# is a no-op that proceeds straight to prepare.


def test_enabled_services_step_empty_proceeds_to_prepare(gitrepo, db, monkeypatch):
    """No services declared (the gitrepo fixture has none) -> the services
    step is a true no-op: run_step is never even called for it, and
    verify_run proceeds straight through cleanup -> prepare -> verify."""
    calls: list[str] = []
    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "prepare": StepResult(status="ok"),
        "verify": StepResult(status="ok"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "prepare": [("executed", _action_payload(
            "allow", builtin="node-deps-reconcile",
            ok=True, reason="", returncode=0,
        ))],
        "verify": [("executed", _action_payload(
            "allow", run="npm run typecheck && npm test",
            ok=True, reason="", returncode=0, stdout="ok\n", stderr="",
        ))],
    }
    base_fake = _fake_run_step(script, events)

    def _tracking_fake(worktree, profile, step_name, role, perms, **kw):
        calls.append(step_name)
        return base_fake(worktree, profile, step_name, role, perms, **kw)

    monkeypatch.setattr(tools_issues, "run_step", _tracking_fake)

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is True
    assert "services" not in calls
    assert calls == ["cleanup", "refresh", "prepare", "verify"]


def test_enabled_services_down_returns_service_not_ready_failure(gitrepo, db, monkeypatch):
    """A services StepResult of status 'failed' maps to a clear, fast
    verify_run failure ('service not ready: <reason>') — not a hang on the
    approval queue — and records a tests_run-style failure event, same as
    the other enabled-path failures. prepare is never reached."""
    _write_repo_profile(gitrepo, "services:\n  - mongo\n")

    script = {
        "cleanup": StepResult(status="ok"),
        "refresh": StepResult(status="ok"),
        "services": StepResult(status="failed",
                              reason="denied (retry after failure): probe-tcp"),
    }
    events = {
        "cleanup": [("executed", _action_payload(
            "allow", run="git reset --hard && git clean -fd",
            ok=True, reason="", returncode=0,
        ))],
        "services": [("failed", _action_payload(
            "allow", builtin="probe-tcp",
            ok=False, reason="mongo=localhost:27017: [Errno 111] Connection refused",
        ))],
    }
    monkeypatch.setattr(tools_issues, "run_step", _fake_run_step(script, events))

    s = _settings(gitrepo, workflow_profile="enabled")
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert "service not ready" in out["tail"]
    assert "mongo=localhost:27017" in out["tail"]

    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert "service not ready" in tests_run[0]["stderr_tail"]
    assert db.of("deps_reinstalled") == []  # prepare never reached


def test_enabled_services_down_real_probe_fails_fast_not_blocked(gitrepo, db):
    """End-to-end (real load_effective + real run_step for cleanup/services;
    only repo.get_issue/append_log/_agent_stamp are faked by `db`): a down
    default-sourced probe (source='default', from the services: scalar
    expansion) must fail the step immediately via the deny-by-default
    escalation_cb wired in `_verify_run_enabled`, never blocked_on_approval.
    The repo profile is COMMITTED (WP-19 note: untracked files don't survive
    the real cleanup step's `git clean -fd`). `stack: node` is declared
    explicitly since this fixture has no package-lock.json to auto-detect —
    `probe-tcp` only resolves through a real adapter (get_adapter needs a
    non-empty stack)."""
    _write_and_commit_repo_profile(gitrepo, "stack: node\nservices:\n  - mongo\n")
    manifest = gitrepo.parent / "workspace.yaml"
    # Point 'mongo' at a closed port so the probe deterministically fails.
    manifest.write_text("service_endpoints:\n  mongo: 127.0.0.1:54329\n")

    s = _settings(gitrepo, workflow_profile="enabled")
    s.workspace_manifest = str(manifest)
    tools = _issue_tools(object(), s)

    out = tools["verify_run"](1)

    assert out["passed"] is False
    assert out.get("status") != "blocked_on_approval"
    assert "service not ready" in out["tail"]
    assert "127.0.0.1:54329" in out["tail"]

    tests_run = db.of("tests_run")
    assert len(tests_run) == 1
    assert "service not ready" in tests_run[0]["stderr_tail"]
    assert db.of("deps_reinstalled") == []
