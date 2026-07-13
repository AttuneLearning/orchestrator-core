"""Integration tests for the comms-ingestion feature (slice D).

Covers message ingestion, triage decisions, alias resolution, anti-ping-pong
logic, TickSummary counters, and the comms_response gate. Requires a running
Postgres instance with migrations applied; the _clean_db autouse fixture in
conftest.py ensures each test starts from a blank slate.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from orchestrator import repository as repo
from orchestrator.agents.base import GateReview, IssueSpec, TriageDecision
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.models import Goal, Issue


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_engine(settings, pool, reasoner=None, **threshold_overrides):
    """Build an Engine with an independent deep-copy of settings."""
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=reasoner or StubReasoner())


def _event_types(pool, issue_id: int) -> list[str]:
    return [e.event_type for e in repo.issue_timeline(pool, issue_id)]


# --------------------------------------------------------------------------- #
# Custom reasoners
# --------------------------------------------------------------------------- #

class _RejectingReasoner:
    """Rejects every message; delegates all other operations to StubReasoner."""

    def __init__(self):
        self._stub = StubReasoner()

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "") -> list[IssueSpec]:
        return self._stub.decompose_goal(goal, max_subissues)

    def plan_issue(self, issue: Issue) -> str:
        return self._stub.plan_issue(issue)

    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list[dict[str, Any]]] = None) -> GateReview:
        return self._stub.gate_review(issue, gate_type, recent)

    def score_drift(self, issue: Issue,
                    recent: Optional[list[dict[str, Any]]] = None) -> float:
        return self._stub.score_drift(issue, recent)

    def triage_message(self, message: dict[str, Any]) -> TriageDecision:
        return TriageDecision(accept=False, reason="not actionable")


# --------------------------------------------------------------------------- #
# 1. Full happy-path: message → issue → done → response
# --------------------------------------------------------------------------- #

def test_ingest_routes_to_team_pull_pipeline(settings, pool):
    """Comms-ingested cross-team requests run on the team's PULL pipeline (live
    coder), not the verdict pipeline-1 — so they reach a worker instead of
    auto-failing at the reasoner verdict. Unknown teams fall back to the default."""
    engine = _make_engine(settings, pool)
    assert engine._ingest_pipeline_for("backend") == "pull-1"
    assert engine._ingest_pipeline_for("frontend") == "pull-fe"
    assert engine._ingest_pipeline_for("nope") == engine.settings.default_pipeline


def test_request_ingested_to_done_with_response(settings, pool):
    """A frontend→api (alias for backend) message flows end-to-end: ingestion,
    issue creation, gate progression, comms response, and message archival."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    msg = repo.create_message(
        pool, "frontend", "api", "Need /users endpoint", "GET /users",
        priority="high",
    )
    msg_id = msg["id"]

    engine = _make_engine(settings, pool)
    # This test validates the full comms loop on the verdict pipeline. Ingestion now
    # routes team work to its pull pipeline; pin to pipeline-1 so the loop completes
    # deterministically under the StubReasoner (no external worker needed).
    engine._ingest_pipeline_for = lambda team_id: "pipeline-1"
    engine.run()

    # Exactly one issue, team backend, state done, triggered_by_message True
    all_issues = repo.list_issues(pool)
    assert len(all_issues) == 1, f"expected 1 issue, got {len(all_issues)}"
    issue = all_issues[0]
    assert issue.state == "done", f"issue state={issue.state!r}"
    assert issue.team == "backend", f"issue team={issue.team!r}"
    assert issue.triggered_by_message is True
    assert issue.origin_message_id == msg_id

    # Timeline: 5 gate_pass events and 1 comms_response event
    timeline = _event_types(pool, issue.id)
    gate_passes = timeline.count("gate_pass")
    comms_resp_events = timeline.count("comms_response")
    assert gate_passes == 5, f"expected 5 gate_pass events, got {gate_passes}; timeline={timeline}"
    assert comms_resp_events == 1, f"expected 1 comms_response event, got {comms_resp_events}"

    # Original message is archived
    original = repo.get_message(pool, msg_id)
    assert original["status"] == "archived", f"original message status={original['status']!r}"

    # A receipt and final response were sent; only the response is kind=response.
    with pool.connection() as conn:
        from psycopg.rows import dict_row
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
    assert len(rows) == 3, f"expected 3 messages total, got {len(rows)}"
    response_msgs = [r for r in rows if r["kind"] == "response"]
    assert len(response_msgs) == 2
    resp = next(r for r in response_msgs if r["subject"].startswith("Re: "))
    assert resp["status"] == "sent", f"response status={resp['status']!r}"
    assert resp["from_team"] == "backend"
    assert resp["to_team"] == "frontend"
    assert resp["subject"].startswith("Re: "), f"subject={resp['subject']!r}"
    assert resp["issue_id"] == issue.id

    # No pending messages remain
    assert repo.pending_messages(pool) == []

    # The auto-created goal starts with "[comms]" and is done
    all_goals = repo.list_all_goals(pool)
    comms_goals = [g for g in all_goals if g.title.startswith("[comms]")]
    assert len(comms_goals) == 1, f"expected 1 [comms] goal, got {len(comms_goals)}"
    assert comms_goals[0].state == "done", f"comms goal state={comms_goals[0].state!r}"


# --------------------------------------------------------------------------- #
# 2. No ping-pong: responses are never re-ingested
# --------------------------------------------------------------------------- #

def test_no_ping_pong(settings, pool):
    """After scenario 1 completes, a second engine run must not create new issues
    or messages — the response (kind='response') must not be ingested."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    repo.create_message(
        pool, "frontend", "api", "Need /users endpoint", "GET /users",
        priority="high",
    )

    # First run — identical to test 1
    engine1 = _make_engine(settings, pool)
    engine1.run()

    issues_after_first = repo.list_issues(pool)
    with pool.connection() as conn:
        msg_count_after_first = conn.execute("SELECT count(*) FROM messages").fetchone()[0]

    # Second run — should be quiescent immediately
    engine2 = _make_engine(settings, pool)
    engine2.run()

    issues_after_second = repo.list_issues(pool)
    with pool.connection() as conn:
        msg_count_after_second = conn.execute("SELECT count(*) FROM messages").fetchone()[0]

    assert len(issues_after_second) == len(issues_after_first), (
        f"new issues created on second run: "
        f"{len(issues_after_first)} → {len(issues_after_second)}"
    )
    assert msg_count_after_second == msg_count_after_first, (
        f"new messages created on second run: "
        f"{msg_count_after_first} → {msg_count_after_second}"
    )


# --------------------------------------------------------------------------- #
# 3. Rejected triage: no issue, message rejected, no goal
# --------------------------------------------------------------------------- #

def test_rejected_triage(settings, pool):
    """A custom reasoner that rejects every message must leave no issues or goals."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    repo.create_message(pool, "frontend", "backend", "Some request", "Do something")

    engine = _make_engine(settings, pool, reasoner=_RejectingReasoner())
    engine.run()

    assert repo.list_issues(pool) == [], "no issues should be created"
    assert repo.list_all_goals(pool) == [], "no goals should be created"

    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM messages ORDER BY id").fetchone()
    assert row[0] == "rejected", f"message status={row[0]!r}"


# --------------------------------------------------------------------------- #
# 4. Unknown team → message rejected
# --------------------------------------------------------------------------- #

def test_unknown_team_rejected(settings, pool):
    """Messages addressed to a non-existent team are rejected by the engine."""
    repo.create_message(pool, "frontend", "nosuchteam", "Hello", "body text")

    engine = _make_engine(settings, pool)
    engine.run()

    assert repo.list_issues(pool) == [], "no issues should be created for unknown team"

    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM messages ORDER BY id").fetchone()
    assert row[0] == "rejected", f"message status={row[0]!r}"


# --------------------------------------------------------------------------- #
# 5. TickSummary counters: one ingested, one rejected
# --------------------------------------------------------------------------- #

def test_summary_counters(settings, pool):
    """A single tick with one valid and one unknown-team message yields
    ingested==1 and rejected==1."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    repo.create_message(pool, "frontend", "backend", "Valid request", "Do work")
    repo.create_message(pool, "frontend", "nosuchteam", "Bad target", "Nowhere to go")

    engine = _make_engine(settings, pool)
    summary = engine.tick()

    assert summary.ingested == 1, f"ingested={summary.ingested}, expected 1"
    assert summary.rejected == 1, f"rejected={summary.rejected}, expected 1"


# --------------------------------------------------------------------------- #
# 6. Issue team is the RECEIVING team, never the sender
# --------------------------------------------------------------------------- #

def test_issue_stays_local(settings, pool):
    """Issues created from inbound messages belong to the receiving (backend) team."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    repo.create_message(pool, "frontend", "backend", "API request", "Please build it")

    engine = _make_engine(settings, pool)
    engine.run()

    all_issues = repo.list_issues(pool)
    assert len(all_issues) == 1
    assert all_issues[0].team == "backend", (
        f"issue team={all_issues[0].team!r}, expected 'backend' (receiving team)"
    )


# --------------------------------------------------------------------------- #
# 7. Repository guards on triage_message
# --------------------------------------------------------------------------- #

def test_triage_message_repository_guards(settings, pool):
    """triage_message raises ValueError when the message is not pending."""
    msg = repo.create_message(pool, "frontend", "backend", "Request", "Body")

    # First triage succeeds
    repo.triage_message(pool, msg["id"], accept=True)

    # Triaging again (no longer pending) must raise
    with pytest.raises(ValueError):
        repo.triage_message(pool, msg["id"], accept=True)

    # Nonexistent id must also raise
    with pytest.raises(ValueError):
        repo.triage_message(pool, 999999, accept=False)


# --------------------------------------------------------------------------- #
# 8. triggered_by_message without origin_message_id: completes, no response
# --------------------------------------------------------------------------- #

def test_manual_triggered_issue_without_origin_still_completes(settings, pool):
    """An issue with triggered_by_message=True but no origin_message_id should
    still reach 'done'; the comms_response work step is a no-op and no response
    message should be created."""
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    goal = repo.create_goal(pool, "Manual comms goal", "test no origin")
    repo.set_goal_state(pool, goal.id, "active")

    repo.create_issue(
        pool, goal.id, "Fix something", team="backend",
        triggered_by_message=True,
        # origin_message_id intentionally omitted (defaults to None)
    )

    engine = _make_engine(settings, pool)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert len(issues) == 1
    assert issues[0].state == "done", f"issue state={issues[0].state!r}"

    # No response message should have been created
    with pool.connection() as conn:
        msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert msg_count == 0, f"expected no messages, got {msg_count}"
