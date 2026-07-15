"""Pending actions: escalation persistence, approval, and consumption."""

import pytest
from datetime import datetime, timedelta, timezone

from orchestrator import repository as repo


def _issue(pool):
    """Helper to create a goal and issue for testing."""
    goal = repo.create_goal(pool, "Test Goal", "Goal description")
    return repo.create_issue(pool, goal.id, "Test Issue", team="backend")


def _events(pool, issue_id, kind):
    """Helper to extract events of a specific kind from the timeline."""
    return [e for e in repo.issue_timeline(pool, issue_id) if e.event_type == kind]


def test_create_pending_action_without_issue(pool):
    """Create a pending action with no issue_id (no event emitted)."""
    action = repo.create_pending_action(
        pool,
        issue_id=None,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        action_kind="run",
        requested_by="agent",
    )
    assert action["status"] == "pending"
    assert action["issue_id"] is None
    assert action["worktree"] == "/tmp/wt"
    assert action["step"] == "verify"
    assert action["action"] == "npm test"
    assert action["action_kind"] == "run"


def test_create_pending_action_with_issue_emits_event(pool):
    """Create a pending action with issue_id emits action_escalated event."""
    # Create an issue first
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        action_kind="run",
        requested_by="qa-agent",
        phase="authorize",
    )
    assert action["issue_id"] == issue.id
    # Check that action_escalated event was emitted
    events = _events(pool, issue.id, "action_escalated")
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["worktree"] == "/tmp/wt"
    assert ev.payload["step"] == "verify"
    assert ev.payload["action"] == "npm test"
    assert ev.payload["action_kind"] == "run"
    assert ev.payload["requested_by"] == "qa-agent"
    assert ev.payload["phase"] == "authorize"
    assert "expires_at" in ev.payload
    assert ev.payload["machine"] is True


def test_create_pending_action_custom_ttl(pool):
    """Create a pending action with custom ttl_hours."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        ttl_hours=2,
        requested_by="qa-agent",
    )
    # Verify expires_at is approximately 2 hours in the future.
    # repository.create_pending_action returns the raw RETURNING row, so
    # expires_at is already a tz-aware datetime.datetime (not a string).
    now = datetime.now(timezone.utc)
    expires = action["expires_at"]
    diff = (expires - now).total_seconds()
    # Should be roughly 7200 seconds (2 hours), with some tolerance
    assert 7100 < diff < 7300


def test_list_pending_actions_no_expiry(pool):
    """List pending actions when none are expired."""
    issue = _issue(pool)
    repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    actions = repo.list_pending_actions(pool, status="pending")
    assert len(actions) == 1
    assert actions[0]["status"] == "pending"


def test_list_pending_actions_lazy_expiry(pool):
    """List with expired rows: status flips to expired, action_expired event emitted."""
    issue = _issue(pool)
    # Create an action that's already expired by using negative ttl_hours
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        ttl_hours=-1,  # Negative to make it already expired
        requested_by="qa-agent",
    )
    # List pending actions should flip the status and emit event
    pending = repo.list_pending_actions(pool, status="pending")
    assert len(pending) == 0, "Expired action should not appear in pending list"
    expired = repo.list_pending_actions(pool, status="expired")
    assert len(expired) == 1
    assert expired[0]["id"] == action["id"]
    assert expired[0]["status"] == "expired"
    # Check action_expired event was emitted
    events = _events(pool, issue.id, "action_expired")
    assert len(events) == 1
    assert events[0].payload["action_id"] == action["id"]
    assert events[0].payload["machine"] is True


def test_resolve_pending_action_approve(pool):
    """Resolve a pending action to approved status."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    resolved = repo.resolve_pending_action(
        pool,
        action["id"],
        status="approved",
        resolved_by="human",
    )
    assert resolved["status"] == "approved"
    assert resolved["resolved_by"] == "human"
    assert resolved["resolved_at"] is not None
    # Check action_approved event
    events = _events(pool, issue.id, "action_approved")
    assert len(events) == 1
    assert events[0].payload["action_id"] == action["id"]
    assert events[0].payload["resolved_by"] == "human"
    assert events[0].payload["machine"] is True


def test_resolve_pending_action_deny(pool):
    """Resolve a pending action to denied status."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    resolved = repo.resolve_pending_action(
        pool,
        action["id"],
        status="denied",
        resolved_by="human",
    )
    assert resolved["status"] == "denied"
    assert resolved["resolved_by"] == "human"
    # Check action_denied event
    events = _events(pool, issue.id, "action_denied")
    assert len(events) == 1
    assert events[0].payload["action_id"] == action["id"]
    assert events[0].payload["resolved_by"] == "human"
    assert events[0].payload["machine"] is True


def test_resolve_pending_action_invalid_status(pool):
    """Resolve with invalid status raises ValueError."""
    action = repo.create_pending_action(
        pool,
        issue_id=None,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    with pytest.raises(ValueError, match="Invalid status"):
        repo.resolve_pending_action(pool, action["id"], status="executed", resolved_by="human")


def test_resolve_pending_action_not_pending_raises(pool):
    """Resolve a non-pending action raises ValueError."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # First resolve to approved
    repo.resolve_pending_action(pool, action["id"], status="approved", resolved_by="human")
    # Try to resolve again; should fail because it's no longer pending
    with pytest.raises(ValueError, match="not found or not in 'pending' status"):
        repo.resolve_pending_action(pool, action["id"], status="denied", resolved_by="human")


def test_resolve_pending_action_unknown_id_raises(pool):
    """Resolve a non-existent action raises ValueError."""
    with pytest.raises(ValueError, match="not found or not in 'pending' status"):
        repo.resolve_pending_action(pool, 9999, status="approved", resolved_by="human")


def test_find_approved_action_match(pool):
    """Find the newest approved action matching the triple."""
    issue = _issue(pool)
    # Create and approve first action
    action1 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt1",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    repo.resolve_pending_action(pool, action1["id"], status="approved", resolved_by="human")
    # Create and approve second action with same step/action
    action2 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt2",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    repo.resolve_pending_action(pool, action2["id"], status="approved", resolved_by="human")
    # Find should return the newest (action2)
    found = repo.find_approved_action(pool, issue.id, "verify", "npm test")
    assert found is not None
    assert found["id"] == action2["id"]


def test_find_approved_action_no_match(pool):
    """Find approved action with no matching triple returns None."""
    issue = _issue(pool)
    repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # Search for different action that doesn't exist
    found = repo.find_approved_action(pool, issue.id, "cleanup", "git reset")
    assert found is None


def test_find_approved_action_only_approved_status(pool):
    """Find only returns approved actions, not pending."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # Don't resolve; it stays pending
    found = repo.find_approved_action(pool, issue.id, "verify", "npm test")
    assert found is None


def test_consume_approved_action_success(pool):
    """Consume an approved action transitions it to executed."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    repo.resolve_pending_action(pool, action["id"], status="approved", resolved_by="human")
    # Consume the approved action
    consumed = repo.consume_approved_action(pool, action["id"])
    assert consumed["status"] == "executed"
    assert consumed["resolved_at"] is not None
    # Check action_executed event
    events = _events(pool, issue.id, "action_executed")
    assert len(events) == 1
    assert events[0].payload["action_id"] == action["id"]
    assert events[0].payload["machine"] is True


def test_consume_approved_action_one_shot(pool):
    """Consume is one-shot: second consume raises ValueError."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    repo.resolve_pending_action(pool, action["id"], status="approved", resolved_by="human")
    # First consume succeeds
    repo.consume_approved_action(pool, action["id"])
    # Second consume fails
    with pytest.raises(ValueError, match="not found or not in 'approved' status"):
        repo.consume_approved_action(pool, action["id"])


def test_consume_approved_action_not_approved_raises(pool):
    """Consume a non-approved action raises ValueError."""
    action = repo.create_pending_action(
        pool,
        issue_id=None,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # Action is still pending, not approved
    with pytest.raises(ValueError, match="not found or not in 'approved' status"):
        repo.consume_approved_action(pool, action["id"])


def test_consume_approved_action_unknown_id_raises(pool):
    """Consume a non-existent action raises ValueError."""
    with pytest.raises(ValueError, match="not found or not in 'approved' status"):
        repo.consume_approved_action(pool, 9999)


def test_pending_actions_all_event_kinds(pool):
    """Verify all five event kinds are emitted correctly."""
    issue = _issue(pool)
    # 1. action_escalated (on create)
    action1 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt1",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # 2. action_approved
    repo.resolve_pending_action(pool, action1["id"], status="approved", resolved_by="human")
    # 3. action_executed (on consume)
    repo.consume_approved_action(pool, action1["id"])
    # 4. action_denied
    action2 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt2",
        step="cleanup",
        action="git reset",
        requested_by="qa-agent",
    )
    repo.resolve_pending_action(pool, action2["id"], status="denied", resolved_by="human")
    # 5. action_expired
    action3 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt3",
        step="prepare",
        action="npm ci",
        ttl_hours=-1,
        requested_by="qa-agent",
    )
    repo.list_pending_actions(pool, status="pending")
    # Check all five event kinds exist and all have machine: True
    all_events = repo.issue_timeline(pool, issue.id)
    event_types = {e.event_type for e in all_events}
    assert "action_escalated" in event_types
    assert "action_approved" in event_types
    assert "action_executed" in event_types
    assert "action_denied" in event_types
    assert "action_expired" in event_types

    # Verify all pending action events have machine: True
    for event in all_events:
        if event.event_type in ("action_escalated", "action_approved", "action_executed",
                                "action_denied", "action_expired"):
            assert event.payload.get("machine") is True, \
                f"Event {event.event_type} missing machine=True"


def test_list_pending_actions_expiry_creates_message(pool):
    """Expiring row creates exactly one orchestration message alongside the event."""
    issue = _issue(pool)
    action = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        ttl_hours=-1,
        requested_by="qa-agent",
    )
    # Before listing, no messages should exist for this issue
    msgs_before = repo.pending_messages(pool, to_team="orchestration")
    issue_msgs_before = [m for m in msgs_before if m["issue_id"] == issue.id]
    assert len(issue_msgs_before) == 0

    # List pending actions (triggers lazy expiry)
    repo.list_pending_actions(pool, status="pending")

    # Now check that exactly one message was created
    msgs_after = repo.pending_messages(pool, to_team="orchestration")
    issue_msgs = [m for m in msgs_after if m["issue_id"] == issue.id]
    assert len(issue_msgs) == 1

    msg = issue_msgs[0]
    assert msg["from_team"] == "engine"
    assert msg["to_team"] == "orchestration"
    assert msg["priority"] == "high"
    assert msg["status"] == "pending"
    assert "verify" in msg["subject"]
    assert "EXPIRED" in msg["subject"]
    assert "npm test" in msg["body"]
    assert "/tmp/wt" in msg["body"]
    assert "qa-agent" in msg["body"]

    # Also check that action_expired event was created
    events = _events(pool, issue.id, "action_expired")
    assert len(events) == 1


def test_list_pending_actions_no_expiry_no_messages(pool):
    """Non-expiring list calls create no messages."""
    issue = _issue(pool)
    repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt",
        step="verify",
        action="npm test",
        requested_by="qa-agent",
    )
    # List with a non-expired action
    repo.list_pending_actions(pool, status="pending")

    # No messages should have been created
    msgs = repo.pending_messages(pool, to_team="orchestration")
    issue_msgs = [m for m in msgs if m["issue_id"] == issue.id]
    assert len(issue_msgs) == 0


def test_list_pending_actions_multiple_expirations_multiple_messages(pool):
    """Two expired rows create two messages."""
    issue = _issue(pool)
    action1 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt1",
        step="verify",
        action="npm test",
        ttl_hours=-1,
        requested_by="qa-agent",
    )
    action2 = repo.create_pending_action(
        pool,
        issue_id=issue.id,
        worktree="/tmp/wt2",
        step="prepare",
        action="npm ci",
        ttl_hours=-1,
        requested_by="agent2",
    )
    # List pending actions (triggers lazy expiry for both)
    repo.list_pending_actions(pool, status="pending")

    # Check that exactly two messages were created for this issue
    msgs = repo.pending_messages(pool, to_team="orchestration")
    issue_msgs = [m for m in msgs if m["issue_id"] == issue.id]
    assert len(issue_msgs) == 2

    # Verify both have the right structure
    for msg in issue_msgs:
        assert msg["from_team"] == "engine"
        assert msg["priority"] == "high"
        assert "EXPIRED" in msg["subject"]
