"""Engine: the contract_check gate (contract-first triage on pull-fe).

Covers the mechanical block (missing contract → message backend + block), the
contract-status unblock (parallel: release on agreement, not backend completion),
pass-through when satisfied, and the fail-open guards (gate off / wrong work_type
/ reasoner capability absent → no-op pass, so old reasoners keep working).
"""

from __future__ import annotations

import copy

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.models import IssueState


def _engine(settings, pool, *, gate_enabled=True, reasoner=None):
    s = copy.deepcopy(settings)
    s.contract_gate_enabled = gate_enabled
    return Engine(s, pool, reasoner=reasoner or StubReasoner())


def _fe_issue_at_contract_check(pool, *, work_type="new-endpoint",
                                desc="Consume the GET /system/status endpoint."):
    """A frontend issue parked in_progress at the contract_check gate."""
    goal = repo.create_goal(pool, "Dashboard", pipeline="pull-fe", state="active")
    issue = repo.create_issue(pool, goal.id, "Wire System Health panel", desc,
                              team="frontend", pipeline="pull-fe")
    repo.set_work_type(pool, issue.id, work_type)
    repo.update_state(pool, issue.id, IssueState.READY.value)
    repo.update_state(pool, issue.id, IssueState.IN_PROGRESS.value,
                      gate_type="contract_check")
    return repo.get_issue(pool, issue.id)


def test_missing_contract_blocks_and_messages_backend(settings, pool):
    eng = _engine(settings, pool)
    issue = _fe_issue_at_contract_check(pool)

    summary = TickSummary()
    blocked = eng._run_contract_check(issue, summary)

    assert blocked is True and summary.contract_blocked == 1
    assert repo.get_issue(pool, issue.id).state == IssueState.BLOCKED.value
    # A proposed contract was recorded and backend was asked for it.
    assert repo.get_contract(pool, "GET", "/system/status")["status"] == "proposed"
    pending = repo.pending_messages(pool, to_team="backend")
    assert len(pending) == 1 and pending[0]["issue_id"] == issue.id
    deps = repo.list_issue_contract_deps(pool, issue.id)
    assert [(d["method"], d["path"]) for d in deps] == [("GET", "/system/status")]


def test_unblocks_on_contract_agreement_not_completion(settings, pool):
    eng = _engine(settings, pool)
    issue = _fe_issue_at_contract_check(pool)
    eng._run_contract_check(issue, TickSummary())
    assert repo.get_issue(pool, issue.id).state == IssueState.BLOCKED.value

    # Still blocked while the contract is only proposed.
    s1 = TickSummary()
    eng._unblock(s1)
    assert repo.get_issue(pool, issue.id).state == IssueState.BLOCKED.value
    assert s1.unblocked == 0

    # Backend agrees the shape (not the implementation) → released to READY.
    repo.set_contract_status(pool, "GET", "/system/status", "agreed")
    s2 = TickSummary()
    eng._unblock(s2)
    assert s2.unblocked == 1
    assert repo.get_issue(pool, issue.id).state == IssueState.READY.value
    assert all(d["satisfied"] for d in repo.list_issue_contract_deps(pool, issue.id))


def test_passes_through_when_contract_already_satisfied(settings, pool):
    repo.upsert_contract(pool, "GET", "/system/status", status="live")
    eng = _engine(settings, pool)
    issue = _fe_issue_at_contract_check(pool)

    blocked = eng._run_contract_check(issue, TickSummary())
    assert blocked is False
    # Not blocked, no spurious message.
    assert repo.get_issue(pool, issue.id).state == IssueState.IN_PROGRESS.value
    assert repo.pending_messages(pool, to_team="backend") == []


def test_fail_open_when_gate_disabled(settings, pool):
    eng = _engine(settings, pool, gate_enabled=False)
    issue = _fe_issue_at_contract_check(pool)
    assert eng._run_contract_check(issue, TickSummary()) is False
    assert repo.pending_messages(pool, to_team="backend") == []


def test_skips_non_endpoint_work_types(settings, pool):
    eng = _engine(settings, pool)
    issue = _fe_issue_at_contract_check(pool, work_type="bug-fix")
    assert eng._run_contract_check(issue, TickSummary()) is False


def _tick_until(eng, pool, issue_id, pred, max_ticks=20):
    for _ in range(max_ticks):
        eng.tick()
        if pred(repo.get_issue(pool, issue_id)):
            return repo.get_issue(pool, issue_id)
    return repo.get_issue(pool, issue_id)


def test_end_to_end_block_then_release_through_pipeline(settings, pool):
    """Full tick loop: a pull-fe new-endpoint issue reaches contract_check, blocks
    on the missing contract, then advances past it once backend agrees."""
    for fn in ("dev", "qa", "lead"):
        repo.register_agent(pool, "frontend", fn)
    eng = _engine(settings, pool)
    goal = repo.create_goal(pool, "Dashboard", "Consume the GET /system/status endpoint.",
                            pipeline="pull-fe")  # backlog → _decompose mints the issue

    # Tick until the goal decomposes into its frontend issue.
    for _ in range(5):
        eng.tick()
        issues = repo.list_issues(pool, goal_id=goal.id)
        if issues:
            break
    issue = issues[0]
    assert issue.team == "frontend" and issue.pipeline == "pull-fe"

    blocked = _tick_until(eng, pool, issue.id,
                          lambda i: i.state == IssueState.BLOCKED.value)
    assert blocked.state == IssueState.BLOCKED.value
    assert blocked.work_type == "new-endpoint"
    assert len(repo.pending_messages(pool, to_team="backend")) == 1
    assert repo.list_issue_contract_deps(pool, issue.id)

    # Backend agrees the contract → the issue clears contract_check and moves on.
    repo.set_contract_status(pool, "GET", "/system/status", "agreed")
    advanced = _tick_until(eng, pool, issue.id,
                           lambda i: i.gate_type == "implementation")
    assert advanced.state != IssueState.BLOCKED.value
    assert advanced.gate_type == "implementation"


def test_fail_open_when_reasoner_lacks_capability(settings, pool):
    from orchestrator.agents.base import GateReview

    class _Bare:
        """An older reasoner with no extract_endpoint_deps capability."""
        def decompose_goal(self, goal, n, rules=""): return []
        def plan_issue(self, issue, rules=""): return ""
        def gate_review(self, *a, **k): return GateReview(passed=True)
        def score_drift(self, *a, **k): return 1.0

    assert not hasattr(_Bare(), "extract_endpoint_deps")
    eng = _engine(settings, pool, reasoner=_Bare())
    issue = _fe_issue_at_contract_check(pool)
    assert eng._run_contract_check(issue, TickSummary()) is False
    assert repo.pending_messages(pool, to_team="backend") == []
