"""Tests for the workflow step runner.

Pure tests: tmp git repos (for sentinel-backed actions) and real subprocess
calls against trivial fake commands (`true`, `false`, `touch <marker>`, a short
sleep for the timeout case) — no network, no npm, no DB.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.workflow import runner
from orchestrator.workflow.models import Profile, RequiredAction, WorkflowStep
from orchestrator.workflow.permissions import Permissions
from orchestrator.workflow.runner import ActionResult, StepResult, run_step


# ---------------------------------------------------------------------------
# Helpers


def _git_init(repo: Path) -> Path:
    """Initialize a bare-bones git repo (no commits needed for these tests)."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    return repo


def _profile_with(step_name: str, actions: tuple[RequiredAction, ...]) -> Profile:
    return Profile(steps={step_name: WorkflowStep(name=step_name, actions=actions)})


def _collector():
    """An event_cb that records (kind, payload) tuples for assertions."""
    events: list[tuple[str, dict]] = []

    def cb(kind: str, payload: dict) -> None:
        # Must be JSON-safe, per the runner's contract.
        json.dumps(payload)
        events.append((kind, payload))

    return events, cb


# ---------------------------------------------------------------------------
# ok path


class TestOkPath:
    def test_all_actions_succeed(self, tmp_path: Path) -> None:
        actions = (
            RequiredAction(run="true", on_fail="block"),
            RequiredAction(run="true", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "ok"
        assert len(result.results) == 2
        assert all(r.ok for r in result.results)
        assert all(r.verdict == "allow" for r in result.results)

    def test_empty_step_is_ok(self, tmp_path: Path) -> None:
        profile = Profile(steps={})
        result = run_step(tmp_path, profile, "verify", None, Permissions())
        assert result.status == "ok"
        assert result.results == []


# ---------------------------------------------------------------------------
# deny


class TestDeny:
    def test_deny_fails_step_and_names_action(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        actions = (
            RequiredAction(run="rm -rf /", on_fail="block"),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(deny=("rm -rf /",), bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "failed"
        assert "rm -rf /" in result.reason
        assert len(result.results) == 1
        assert result.results[0].verdict == "deny"
        assert result.results[0].ok is False
        # Short-circuited: the second action never ran.
        assert not marker.exists()

    def test_deny_beats_bypass(self, tmp_path: Path) -> None:
        actions = (RequiredAction(run="danger", on_fail="warn"),)
        profile = _profile_with("verify", actions)
        perms = Permissions(deny=("danger",), bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "failed"
        assert result.results[0].verdict == "deny"


# ---------------------------------------------------------------------------
# escalate short-circuit


class TestEscalate:
    def test_escalate_blocks_and_skips_later_actions(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker2"
        actions = (
            RequiredAction(run="custom-unapproved-command", on_fail="block"),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions()  # no allow/deny/bypass -> custom run escalates

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "blocked_on_approval"
        assert len(result.results) == 1
        assert result.results[0].verdict == "escalate"
        assert not marker.exists()

    def test_default_escalation_cb_is_none_means_pending(self, tmp_path: Path) -> None:
        actions = (RequiredAction(run="custom-cmd", on_fail="block"),)
        profile = _profile_with("verify", actions)

        result = run_step(tmp_path, profile, "verify", None, Permissions(), escalation_cb=None)

        assert result.status == "blocked_on_approval"

    def test_escalation_cb_approved_lets_action_run(self, tmp_path: Path) -> None:
        marker = tmp_path / "ran"
        actions = (RequiredAction(run=f"touch {marker}", on_fail="block"),)
        profile = _profile_with("verify", actions)

        result = run_step(
            tmp_path, profile, "verify", None, Permissions(),
            escalation_cb=lambda action: "approved",
        )

        assert result.status == "ok"
        assert marker.exists()
        assert result.results[0].ok is True

    def test_escalation_cb_pending_still_blocks(self, tmp_path: Path) -> None:
        actions = (RequiredAction(run="custom-cmd", on_fail="block"),)
        profile = _profile_with("verify", actions)

        result = run_step(
            tmp_path, profile, "verify", None, Permissions(),
            escalation_cb=lambda action: "pending",
        )

        assert result.status == "blocked_on_approval"


# ---------------------------------------------------------------------------
# on_fail: warn vs block


class TestOnFail:
    def test_warn_records_and_continues(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        actions = (
            RequiredAction(run="false", on_fail="warn"),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "ok"
        assert len(result.results) == 2
        assert result.results[0].ok is False
        assert result.results[1].ok is True
        assert marker.exists()

    def test_block_fails_step_now(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        actions = (
            RequiredAction(run="false", on_fail="block"),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "failed"
        assert len(result.results) == 1
        assert not marker.exists()

    def test_on_fail_escalate_goes_through_escalation_cb(self, tmp_path: Path) -> None:
        actions = (RequiredAction(run="false", on_fail="escalate"),)
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "blocked_on_approval"

    def test_on_fail_escalate_approved_continues(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        actions = (
            RequiredAction(run="false", on_fail="escalate"),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(
            tmp_path, profile, "verify", None, perms,
            escalation_cb=lambda action: "approved",
        )

        assert result.status == "ok"
        assert marker.exists()


# ---------------------------------------------------------------------------
# timeout


class TestTimeout:
    def test_timeout_produces_failed_action_not_exception(self, tmp_path: Path) -> None:
        actions = (RequiredAction(run="sleep 3", on_fail="block", timeout=1),)
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "failed"
        assert result.results[0].ok is False
        assert "timed out" in result.results[0].detail.get("reason", "")

    def test_timeout_with_warn_continues(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        actions = (
            RequiredAction(run="sleep 3", on_fail="warn", timeout=1),
            RequiredAction(run=f"touch {marker}", on_fail="block"),
        )
        profile = _profile_with("verify", actions)
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", None, perms)

        assert result.status == "ok"
        assert marker.exists()


# ---------------------------------------------------------------------------
# sentinel skip + re-run after content change


class TestSentinelSkip:
    def test_skip_on_second_run_then_rerun_after_change(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path)
        tracked = repo / "lockfile.txt"
        tracked.write_text("v1")
        marker = repo / "ran-marker"

        action = RequiredAction(
            run=f"touch {marker}",
            when_changed=("lockfile.txt",),
            sentinel="test-sentinel",
            on_fail="block",
        )
        profile = _profile_with("prepare", (action,))
        perms = Permissions(bypass=True)

        # First run: stale (no sentinel yet) -> executes, writes sentinel.
        result1 = run_step(repo, profile, "prepare", None, perms)
        assert result1.status == "ok"
        assert result1.results[0].ok is True
        assert result1.results[0].skipped == ""
        assert marker.exists()

        # Remove the marker to prove the *second* run does NOT re-execute.
        marker.unlink()
        result2 = run_step(repo, profile, "prepare", None, perms)
        assert result2.status == "ok"
        assert result2.results[0].skipped == "unchanged"
        assert not marker.exists()

        # Change the tracked file -> stale again -> executes and recreates marker.
        tracked.write_text("v2")
        result3 = run_step(repo, profile, "prepare", None, perms)
        assert result3.status == "ok"
        assert result3.results[0].skipped == ""
        assert marker.exists()

    def test_sentinel_written_only_after_success(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path)
        (repo / "lockfile.txt").write_text("v1")

        action = RequiredAction(
            run="false",
            when_changed=("lockfile.txt",),
            sentinel="fail-sentinel",
            on_fail="warn",
        )
        profile = _profile_with("prepare", (action,))
        perms = Permissions(bypass=True)

        result = run_step(repo, profile, "prepare", None, perms)
        assert result.status == "ok"
        assert result.results[0].ok is False

        from orchestrator.workflow import sentinel as sentinel_mod

        sentinel_file = sentinel_mod.sentinel_path(repo, "fail-sentinel")
        assert not sentinel_file.is_file()

    def test_no_skip_when_sentinel_or_when_changed_missing(self, tmp_path: Path) -> None:
        """Only when BOTH when_changed and sentinel are set does the skip logic apply."""
        marker = tmp_path / "marker"
        action = RequiredAction(run=f"touch {marker}", when_changed=("*.txt",), on_fail="block")
        profile = _profile_with("prepare", (action,))
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "prepare", None, perms)
        assert result.status == "ok"
        assert result.results[0].skipped == ""
        assert marker.exists()


# ---------------------------------------------------------------------------
# event_cb payloads


class TestEventCb:
    def test_executed_and_skipped_events_fire(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path)
        (repo / "lockfile.txt").write_text("v1")
        events, cb = _collector()

        action = RequiredAction(
            run="true",
            when_changed=("lockfile.txt",),
            sentinel="evt-sentinel",
            on_fail="block",
        )
        profile = _profile_with("prepare", (action,))
        perms = Permissions(bypass=True)

        run_step(repo, profile, "prepare", None, perms, event_cb=cb)
        assert [kind for kind, _ in events] == ["executed"]

        events.clear()
        run_step(repo, profile, "prepare", None, perms, event_cb=cb)
        assert [kind for kind, _ in events] == ["skipped"]
        assert events[0][1]["skipped"] == "unchanged"
        assert events[0][1]["action"]["run"] == "true"

    def test_refused_event_on_deny(self, tmp_path: Path) -> None:
        events, cb = _collector()
        action = RequiredAction(run="danger", on_fail="block")
        profile = _profile_with("verify", (action,))
        perms = Permissions(deny=("danger",))

        run_step(tmp_path, profile, "verify", None, perms, event_cb=cb)

        assert [kind for kind, _ in events] == ["refused"]
        assert events[0][1]["verdict"] == "deny"

    def test_escalated_event_on_escalate(self, tmp_path: Path) -> None:
        events, cb = _collector()
        action = RequiredAction(run="custom-cmd", on_fail="block")
        profile = _profile_with("verify", (action,))

        run_step(tmp_path, profile, "verify", None, Permissions(), event_cb=cb)

        assert [kind for kind, _ in events] == ["escalated"]
        assert events[0][1]["decision"] == "pending"

    def test_failed_event_on_failure(self, tmp_path: Path) -> None:
        events, cb = _collector()
        action = RequiredAction(run="false", on_fail="warn")
        profile = _profile_with("verify", (action,))
        perms = Permissions(bypass=True)

        run_step(tmp_path, profile, "verify", None, perms, event_cb=cb)

        assert [kind for kind, _ in events] == ["failed"]
        assert events[0][1]["ok"] is False


# ---------------------------------------------------------------------------
# role resolution + builtin dispatch


class TestRoleResolution:
    def test_role_specific_actions_used_when_present(self, tmp_path: Path) -> None:
        marker = tmp_path / "qa-marker"
        step = WorkflowStep(
            name="verify",
            actions=(RequiredAction(run="true"),),
            by_role={"qa": (RequiredAction(run=f"touch {marker}"),)},
        )
        profile = Profile(steps={"verify": step})
        perms = Permissions(bypass=True)

        result = run_step(tmp_path, profile, "verify", "qa", perms)

        assert result.status == "ok"
        assert marker.exists()


class TestBuiltinDispatch:
    def test_builtin_executes_via_adapter_and_authorizes_by_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = []

        class FakeAdapter:
            def builtins(self):
                def _noop_ok(worktree):
                    calls.append(worktree)
                    return {"ok": True, "reason": "fine"}

                return {"noop-ok": _noop_ok}

        monkeypatch.setattr(runner, "get_adapter", lambda stack: FakeAdapter())

        action = RequiredAction(builtin="noop-ok", on_fail="block")
        profile = _profile_with("prepare", (action,))
        profile = Profile(stack="fake", steps=profile.steps)

        # Empty perms: builtins authorize by identity regardless of allow list.
        result = run_step(tmp_path, profile, "prepare", None, Permissions())

        assert result.status == "ok"
        assert result.results[0].verdict == "allow"
        assert result.results[0].detail["reason"] == "fine"
        assert calls == [tmp_path]

    def test_builtin_failure_honors_on_fail_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeAdapter:
            def builtins(self):
                return {"noop-fail": lambda worktree: {"ok": False, "reason": "boom"}}

        monkeypatch.setattr(runner, "get_adapter", lambda stack: FakeAdapter())

        action = RequiredAction(builtin="noop-fail", on_fail="block")
        profile = Profile(stack="fake", steps={"prepare": WorkflowStep(name="prepare", actions=(action,))})

        result = run_step(tmp_path, profile, "prepare", None, Permissions())

        assert result.status == "failed"
        assert result.results[0].detail["reason"] == "boom"

    def test_unknown_stack_adapter_fails_gracefully(self, tmp_path: Path) -> None:
        action = RequiredAction(builtin="whatever", on_fail="block")
        profile = Profile(stack="", steps={"prepare": WorkflowStep(name="prepare", actions=(action,))})

        result = run_step(tmp_path, profile, "prepare", None, Permissions())

        assert result.status == "failed"
        assert "no adapter" in result.results[0].detail["reason"]

    def test_unknown_builtin_name_fails_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeAdapter:
            def builtins(self):
                return {}

        monkeypatch.setattr(runner, "get_adapter", lambda stack: FakeAdapter())

        action = RequiredAction(builtin="ghost", on_fail="block")
        profile = Profile(stack="fake", steps={"prepare": WorkflowStep(name="prepare", actions=(action,))})

        result = run_step(tmp_path, profile, "prepare", None, Permissions())

        assert result.status == "failed"
        assert "unknown builtin" in result.results[0].detail["reason"]

    def test_builtin_raising_exception_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeAdapter:
            def builtins(self):
                def _boom(worktree):
                    raise RuntimeError("kaboom")

                return {"boom": _boom}

        monkeypatch.setattr(runner, "get_adapter", lambda stack: FakeAdapter())

        action = RequiredAction(builtin="boom", on_fail="warn")
        profile = Profile(stack="fake", steps={"prepare": WorkflowStep(name="prepare", actions=(action,))})

        result = run_step(tmp_path, profile, "prepare", None, Permissions())

        assert result.status == "ok"  # on_fail=warn -> continues
        assert result.results[0].ok is False
        assert "kaboom" in result.results[0].detail["reason"]
