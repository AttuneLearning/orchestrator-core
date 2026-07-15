"""DB-backed tests for orchestrator/workflow/escalation.py (WP-12).

Uses the `pool` fixture (tests/conftest.py) — per FANOUT-CLAUDE.md this file
is written by the WP agent but NEVER executed by it; self-check with
`pytest --collect-only` only. The monitor runs it for real, serially, at a
wave/gate boundary.

Deliberately in its own file (tests/test_workflow_escalation.py), NOT
tests/test_pending_actions.py — another agent (WP-11) owns that file
concurrently.
"""

from __future__ import annotations

import pytest

from orchestrator import repository as repo
from orchestrator.workflow.escalation import handle_escalation, make_escalation_cb
from orchestrator.workflow.models import RequiredAction


def _make_issue(pool, team: str = "backend"):
    goal = repo.create_goal(pool, "Escalation test goal")
    return repo.create_issue(pool, goal.id, "Escalation test issue", team=team)


class TestFirstEscalation:
    def test_creates_pending_row_and_orchestration_message(self, pool) -> None:
        issue = _make_issue(pool, team="backend")

        decision = handle_escalation(
            pool,
            issue.id,
            "/tmp/wt",
            "verify",
            "npm run something-custom",
            "run",
            "dev-worker",
            "authorize",
        )

        assert decision == "pending"

        rows = repo.list_pending_actions(pool, status="pending")
        matching = [r for r in rows if r["issue_id"] == issue.id]
        assert len(matching) == 1
        row = matching[0]
        assert row["step"] == "verify"
        assert row["action"] == "npm run something-custom"
        assert row["action_kind"] == "run"
        # phase folded into requested_by (no migration; see escalation.py docstring)
        assert "dev-worker" in row["requested_by"]
        assert "phase:authorize" in row["requested_by"]

        # action_escalated event carries the phase as a first-class key
        # and also phase-tagged requested_by for row-level visibility.
        events = repo.recent_events(pool, issue.id)
        escalated = [e for e in events if e.event_type == "action_escalated"]
        assert len(escalated) == 1
        assert escalated[0].payload["phase"] == "authorize"
        assert "phase:authorize" in escalated[0].payload["requested_by"]
        assert escalated[0].payload["machine"] is True

        # comms message sent to the orchestration team, high priority, with
        # phase explained in the body.
        pending_msgs = repo.pending_messages(pool, to_team="orchestration")
        matching_msgs = [m for m in pending_msgs if m["issue_id"] == issue.id]
        assert len(matching_msgs) == 1
        msg = matching_msgs[0]
        assert msg["priority"] == "high"
        assert msg["from_team"] == "backend"
        assert "verify" in msg["subject"]
        assert "authorize" in msg["body"]
        assert "npm run something-custom" in msg["body"]

    def test_on_fail_phase_explained_differently_in_message_body(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "flaky-cmd", "run", "qa-worker", "on_fail",
        )

        msgs = repo.pending_messages(pool, to_team="orchestration")
        msg = [m for m in msgs if m["issue_id"] == issue.id][0]
        assert "on_fail" in msg["body"]
        assert "retry" in msg["body"].lower()

        rows = repo.list_pending_actions(pool, status="pending")
        row = [r for r in rows if r["issue_id"] == issue.id][0]
        assert "phase:on_fail" in row["requested_by"]


class TestDuplicatePending:
    def test_second_call_same_issue_step_action_does_not_duplicate(self, pool) -> None:
        issue = _make_issue(pool)

        first = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "same-cmd", "run", "dev", "authorize",
        )
        second = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "same-cmd", "run", "dev", "authorize",
        )

        assert first == "pending"
        assert second == "pending"

        rows = repo.list_pending_actions(pool, status="pending")
        matching = [r for r in rows if r["issue_id"] == issue.id and r["action"] == "same-cmd"]
        assert len(matching) == 1  # no duplicate row

        msgs = repo.pending_messages(pool, to_team="orchestration")
        matching_msgs = [m for m in msgs if m["issue_id"] == issue.id]
        assert len(matching_msgs) == 1  # no duplicate message

    def test_different_action_same_issue_step_does_create_new_row(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "cmd-a", "run", "dev", "authorize")
        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "cmd-b", "run", "dev", "authorize")

        rows = repo.list_pending_actions(pool, status="pending")
        matching = [r for r in rows if r["issue_id"] == issue.id]
        assert len(matching) == 2


class TestApprovedConsumption:
    def test_approved_row_consumed_and_returns_approved(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "some-cmd", "run", "dev", "authorize")
        pending = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id
        ][0]
        repo.resolve_pending_action(pool, pending["id"], "approved", resolved_by="alice")

        decision = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "some-cmd", "run", "dev", "authorize",
        )

        assert decision == "approved"

        # one-shot: the approved row is now consumed (executed), not sitting
        # around as approved/pending.
        executed_rows = [
            r for r in repo.list_pending_actions(pool, status="executed")
            if r["issue_id"] == issue.id
        ]
        assert len(executed_rows) == 1
        assert executed_rows[0]["id"] == pending["id"]

        approved_events = [
            e for e in repo.recent_events(pool, issue.id) if e.event_type == "action_executed"
        ]
        assert len(approved_events) == 1
        assert approved_events[0].payload["machine"] is True

    def test_one_shot_second_call_after_consume_re_escalates(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "one-shot-cmd", "run", "dev", "authorize")
        pending = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id
        ][0]
        repo.resolve_pending_action(pool, pending["id"], "approved", resolved_by="alice")

        first = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "one-shot-cmd", "run", "dev", "authorize",
        )
        assert first == "approved"

        # No new approval granted -> re-escalates (creates a fresh pending row).
        second = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "one-shot-cmd", "run", "dev", "authorize",
        )
        assert second == "pending"

        pending_rows = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id and r["action"] == "one-shot-cmd"
        ]
        assert len(pending_rows) == 1  # the re-escalation, a brand new row

        # Two comms messages total now: the original escalation, and the
        # re-escalation after the one-shot approval was consumed.
        msgs = repo.pending_messages(pool, to_team="orchestration")
        matching_msgs = [
            m for m in msgs if m["issue_id"] == issue.id and "one-shot-cmd" in m["subject"]
        ]
        assert len(matching_msgs) == 2


class TestMakeEscalationCb:
    def test_returned_closure_matches_run_step_contract(self, pool) -> None:
        issue = _make_issue(pool)
        cb = make_escalation_cb(pool, issue.id, "/tmp/wt", "verify", "dev-worker")

        action = RequiredAction(run="closure-cmd", on_fail="block", source="repo")
        decision = cb(action, "authorize")

        assert decision == "pending"
        rows = repo.list_pending_actions(pool, status="pending")
        matching = [r for r in rows if r["issue_id"] == issue.id and r["action"] == "closure-cmd"]
        assert len(matching) == 1

    def test_builtin_action_identity_uses_builtin_name(self, pool) -> None:
        issue = _make_issue(pool)
        cb = make_escalation_cb(pool, issue.id, "/tmp/wt", "prepare", "dev-worker")

        action = RequiredAction(builtin="node-deps-reconcile", on_fail="escalate")
        cb(action, "on_fail")

        rows = repo.list_pending_actions(pool, status="pending")
        matching = [r for r in rows if r["issue_id"] == issue.id]
        assert len(matching) == 1
        assert matching[0]["action"] == "node-deps-reconcile"
        assert matching[0]["action_kind"] == "builtin"


class TestDeniedIsAHardStop:
    """WP-13: a denied row must never re-escalate — the runner turns "denied"
    into a real failed step, not another pending row."""

    def test_denied_row_returns_denied_and_creates_no_new_pending_row(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "denied-cmd", "run", "dev", "authorize")
        pending = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id
        ][0]
        repo.resolve_pending_action(pool, pending["id"], "denied", resolved_by="bob")

        decision = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "denied-cmd", "run", "dev", "authorize",
        )

        assert decision == "denied"

        # Hard stop: no new pending row was created for the same triple.
        pending_rows = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id and r["action"] == "denied-cmd"
        ]
        assert pending_rows == []

        denied_rows = [
            r for r in repo.list_pending_actions(pool, status="denied")
            if r["issue_id"] == issue.id and r["action"] == "denied-cmd"
        ]
        assert len(denied_rows) == 1  # still just the one denied row

        # Check that action_denied event has machine: True
        denied_events = [
            e for e in repo.recent_events(pool, issue.id) if e.event_type == "action_denied"
        ]
        assert len(denied_events) == 1
        assert denied_events[0].payload["machine"] is True

    def test_repeated_calls_after_denial_keep_returning_denied(self, pool) -> None:
        issue = _make_issue(pool)

        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "denied-cmd-2", "run", "dev", "authorize")
        pending = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id
        ][0]
        repo.resolve_pending_action(pool, pending["id"], "denied", resolved_by="bob")

        first = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "denied-cmd-2", "run", "dev", "authorize",
        )
        second = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "denied-cmd-2", "run", "dev", "authorize",
        )

        assert first == "denied"
        assert second == "denied"

    def test_approval_older_than_denial_still_denies(self, pool) -> None:
        """Newest resolution wins: even if an (unconsumed) approved row exists
        for this triple, a NEWER denied row takes precedence — built directly
        via repository.py (not two handle_escalation calls) because the FIRST
        thing handle_escalation does on any call is check for an approved row
        and consume it immediately, so it can't be used to create a second,
        independent row for the same triple while the first is still
        unconsumed-approved."""
        issue = _make_issue(pool)

        older = repo.create_pending_action(
            pool, issue_id=issue.id, worktree="/tmp/wt", step="verify",
            action="race-cmd", action_kind="run", requested_by="dev",
        )
        repo.resolve_pending_action(pool, older["id"], "approved", resolved_by="alice")

        newer = repo.create_pending_action(
            pool, issue_id=issue.id, worktree="/tmp/wt", step="verify",
            action="race-cmd", action_kind="run", requested_by="dev",
        )
        repo.resolve_pending_action(pool, newer["id"], "denied", resolved_by="bob")

        decision = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "race-cmd", "run", "dev", "authorize",
        )

        assert decision == "denied"


class TestNoIssueId:
    def test_issue_id_none_skips_dedup_lookup_and_uses_engine_team(self, pool) -> None:
        # issue_id=None: no issue to scope pending-row lookups or events to;
        # from_team falls back to "engine" since there's no Issue to read
        # .team from.
        decision = handle_escalation(
            pool, None, "/tmp/wt", "verify", "orphan-cmd", "run", "dev", "authorize",
        )
        assert decision == "pending"

        msgs = repo.pending_messages(pool, to_team="orchestration")
        matching = [m for m in msgs if "orphan-cmd" in m["subject"]]
        assert len(matching) == 1
        assert matching[0]["from_team"] == "engine"
        assert matching[0]["issue_id"] is None


class TestConcurrentConsumeRace:
    """NIT 1: concurrent-consume race in handle_escalation.

    The approved path calls consume_approved_action, which raises ValueError
    if another worker consumed the row between find_approved_action and consume
    (atomic UPDATE guarantees no double-execution; the loser just errors).
    Fix: catch ValueError and return "pending" so the loser re-escalates
    cleanly on its next poll — same behavior as already-consumed approval."""

    def test_concurrent_consume_returns_pending_instead_of_raising(self, pool) -> None:
        issue = _make_issue(pool)

        # Approve a row
        handle_escalation(pool, issue.id, "/tmp/wt", "verify", "race-cmd", "run", "dev", "authorize")
        pending = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id
        ][0]
        repo.resolve_pending_action(pool, pending["id"], "approved", resolved_by="alice")

        # Consume it directly (simulating another worker winning the race)
        repo.consume_approved_action(pool, pending["id"])

        # The loser calls handle_escalation for the same triple; should get "pending"
        # (a new pending row), not raise ValueError
        decision = handle_escalation(
            pool, issue.id, "/tmp/wt", "verify", "race-cmd", "run", "dev", "authorize",
        )

        assert decision == "pending"

        # A new pending row was created (re-escalation)
        pending_rows = [
            r for r in repo.list_pending_actions(pool, status="pending")
            if r["issue_id"] == issue.id and r["action"] == "race-cmd"
        ]
        assert len(pending_rows) == 1
