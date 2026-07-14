"""Tests for the ADR-governance feature (test_adr_governance.py).

Covers: pure rule selection (detect_work_type, applicable, format_rules_block,
reverse_links), DB-backed CRUD and lifecycle, engine integration (work_type
tagging, rule injection, old-signature fallback, violated_rules, gap proposals),
and dashboard routes.

Requires a running Postgres instance with migrations applied for the DB-backed
tests.  Pure tests are grouped without the pool fixture and run in-process.
"""

from __future__ import annotations

import copy
import warnings
from typing import Any, Optional

warnings.filterwarnings("ignore")

import pytest
from fastapi.testclient import TestClient

from orchestrator import repository as repo
from orchestrator.adr_rules import (
    applicable,
    detect_work_type,
    format_rules_block,
    reverse_links,
)
from orchestrator.agents.base import GateReview, IssueSpec
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.dashboard.app import create_app
from orchestrator.engine.loop import Engine
from orchestrator.models import Goal, Issue


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_engine(settings, pool, reasoner=None, **threshold_overrides):
    s = copy.deepcopy(settings)
    for k, v in threshold_overrides.items():
        setattr(s.thresholds, k, v)
    return Engine(s, pool, reasoner=reasoner or StubReasoner())


def _rule(
    adr_key: str,
    decision: str = "Follow the rule.",
    status: str = "accepted",
    applies_to: Optional[dict] = None,
    related: Optional[list] = None,
    supersedes: Optional[list] = None,
) -> dict[str, Any]:
    """Build a minimal rule dict for pure-function tests."""
    return {
        "adr_key": adr_key,
        "decision": decision,
        "status": status,
        "applies_to": applies_to or {},
        "related": related or [],
        "supersedes": supersedes or [],
    }


def _events_of_type(pool, issue_id: int, event_type: str):
    return [
        e for e in repo.recent_events(pool, issue_id, limit=200)
        if e.event_type == event_type
    ]


# --------------------------------------------------------------------------- #
# 1. detect_work_type — pure, no DB
# --------------------------------------------------------------------------- #

class TestDetectWorkType:
    def test_auth_beats_others(self):
        # "login" and "role" and "permissions" are all auth-change keywords;
        # auth-change is first in the priority list so it wins.
        assert detect_work_type("Add login role permissions") == "auth-change"

    def test_new_endpoint(self):
        assert detect_work_type("Add an endpoint route") == "new-endpoint"

    def test_bug_fix(self):
        assert detect_work_type("Fix crash") == "bug-fix"

    def test_general_fallback(self):
        # "Refactor docs" contains no keyword from any bucket.
        assert detect_work_type("Refactor docs") == "general"


# --------------------------------------------------------------------------- #
# 2. applicable() — pure, no DB
# --------------------------------------------------------------------------- #

class TestApplicable:
    def test_proposed_rule_excluded(self):
        rule = _rule("ADR-X-001", status="proposed")
        assert applicable([rule], work_type="general", team="backend") == []

    def test_empty_applies_to_matches_anything(self):
        rule = _rule("ADR-X-001", applies_to={})
        out = applicable([rule], work_type="auth-change", team="frontend",
                         repos=["web-repo"])
        assert len(out) == 1

    def test_work_type_mismatch_excluded(self):
        rule = _rule("ADR-X-001", applies_to={"work_types": ["bug-fix"]})
        out = applicable([rule], work_type="new-endpoint", team="backend",
                         repos=["api-repo"])
        assert out == []

    def test_team_list_respected(self):
        rule = _rule("ADR-X-001", applies_to={"teams": ["frontend"]})
        # backend team is excluded
        assert applicable([rule], work_type="general", team="backend") == []
        # frontend team matches
        assert len(applicable([rule], work_type="general", team="frontend")) == 1

    def test_repo_scoped_rule_matches_team_repo_intersection(self):
        rule = _rule("ADR-X-001", applies_to={"repos": ["api-repo"]})
        # backend has api-repo → match
        out = applicable([rule], work_type="general", team="backend",
                         repos=["api-repo"])
        assert len(out) == 1

    def test_repo_scoped_rule_does_not_reach_empty_repos_team(self):
        # qa team has repos=[] — no repo intersection with a repo-scoped rule
        rule = _rule("ADR-X-001", applies_to={"repos": ["api-repo"]})
        out = applicable([rule], work_type="general", team="qa", repos=[])
        assert out == []

    def test_project_wide_rule_reaches_everyone(self):
        # applies_to.repos empty → project-wide → matches any team
        rule = _rule("ADR-X-001", applies_to={"repos": []})
        assert len(applicable([rule], work_type="general", team="qa",
                               repos=[])) == 1
        assert len(applicable([rule], work_type="general", team="backend",
                               repos=["api-repo"])) == 1


# --------------------------------------------------------------------------- #
# 3. format_rules_block — pure, no DB
# --------------------------------------------------------------------------- #

class TestFormatRulesBlock:
    def test_empty_list_returns_empty_string(self):
        assert format_rules_block([]) == ""

    def test_non_empty_contains_header_and_entries(self):
        rules = [
            _rule("ADR-API-001", decision="Always write tests."),
            _rule("ADR-API-002", decision="Use snake_case."),
        ]
        block = format_rules_block(rules)
        assert block != ""
        assert "## Applicable rules" in block
        assert "- [ADR-API-001] Always write tests." in block
        assert "- [ADR-API-002] Use snake_case." in block


# --------------------------------------------------------------------------- #
# 4. reverse_links — pure, no DB
# --------------------------------------------------------------------------- #

class TestReverseLinks:
    def test_related_and_supersedes_backlinks(self):
        a = _rule("ADR-A-001", related=["ADR-B-001"])
        c = _rule("ADR-C-001", supersedes=["ADR-B-001"])
        incoming = reverse_links([a, c])
        # Both A and C point at B; order-insensitive check
        assert set(incoming["ADR-B-001"]) == {"ADR-A-001", "ADR-C-001"}


# --------------------------------------------------------------------------- #
# 5. create / list / get — DB-backed
# --------------------------------------------------------------------------- #

def test_create_list_get_adrs(pool):
    # Two in domain 'api' → ADR-API-001 and ADR-API-002
    a1 = repo.create_adr(pool, "api", "First rule", decision="Do A.",
                         context="Because A.", proposed_by="alice",
                         applies_to={"teams": ["backend"]},
                         related=["ADR-PROC-001"])
    a2 = repo.create_adr(pool, "api", "Second rule", decision="Do B.",
                         status="proposed")
    # One in domain 'proc' → ADR-PROC-001
    p1 = repo.create_adr(pool, "proc", "Proc rule", decision="Do P.")

    assert a1["adr_key"] == "ADR-API-001"
    assert a2["adr_key"] == "ADR-API-002"
    assert p1["adr_key"] == "ADR-PROC-001"

    # list_adrs with status filter
    accepted = repo.list_adrs(pool, status="accepted")
    proposed = repo.list_adrs(pool, status="proposed")
    assert any(r["adr_key"] == "ADR-API-001" for r in accepted)
    assert any(r["adr_key"] == "ADR-API-002" for r in proposed)

    # list_adrs with domain filter
    api_rules = repo.list_adrs(pool, domain="api")
    assert len(api_rules) == 2
    proc_rules = repo.list_adrs(pool, domain="proc")
    assert len(proc_rules) == 1

    # get_adr returns full dict
    fetched = repo.get_adr(pool, "ADR-API-001")
    assert fetched is not None
    assert fetched["applies_to"]["teams"] == ["backend"]
    assert fetched["related"] == ["ADR-PROC-001"]
    assert fetched["proposed_by"] == "alice"
    assert fetched["decision"] == "Do A."
    assert fetched["context"] == "Because A."


# --------------------------------------------------------------------------- #
# 6. approve lifecycle — DB-backed
# --------------------------------------------------------------------------- #

def test_approve_lifecycle(pool):
    adr = repo.create_adr(pool, "api", "Approval test", decision="Do X.",
                          status="proposed")
    key = adr["adr_key"]

    # proposed → approve → accepted
    approved = repo.approve_adr(pool, key)
    assert approved["status"] == "accepted"

    fetched = repo.get_adr(pool, key)
    assert fetched["status"] == "accepted"

    # Approving an accepted ADR raises ValueError (not in 'proposed' status)
    with pytest.raises(ValueError):
        repo.approve_adr(pool, key)

    # Approving an unknown key raises ValueError
    with pytest.raises(ValueError):
        repo.approve_adr(pool, "ADR-NONE-999")


# --------------------------------------------------------------------------- #
# 7. approve supersedes — DB-backed
# --------------------------------------------------------------------------- #

def test_approve_supersedes(pool):
    old = repo.create_adr(pool, "api", "Old rule", decision="Old directive.",
                          status="accepted")
    old_key = old["adr_key"]

    new = repo.create_adr(pool, "api", "New rule", decision="New directive.",
                          status="proposed", supersedes=[old_key])
    new_key = new["adr_key"]

    repo.approve_adr(pool, new_key)

    assert repo.get_adr(pool, new_key)["status"] == "accepted"
    assert repo.get_adr(pool, old_key)["status"] == "superseded"


# --------------------------------------------------------------------------- #
# 8. engine tags work_type and logs rules — DB-backed
# --------------------------------------------------------------------------- #

def test_engine_tags_work_type_and_logs_rules(settings, pool):
    # Project-wide accepted rule (empty applies_to → matches every issue)
    rule = repo.create_adr(pool, "api", "Endpoint rule",
                           decision="Always add OpenAPI docs.",
                           status="accepted",
                           applies_to={})
    rule_key = rule["adr_key"]

    goal = repo.create_goal(pool, "Add a new endpoint for certificates",
                            "New REST endpoint for cert management.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=StubReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    # Find the "Implement:" issue created by StubReasoner
    impl_issue = next(
        (i for i in issues if i.title.startswith("Implement:")), None
    )
    assert impl_issue is not None, "StubReasoner should produce an Implement issue"

    # work_type should be 'new-endpoint' (goal title contains "endpoint")
    assert impl_issue.work_type == "new-endpoint", (
        f"expected work_type='new-endpoint', got {impl_issue.work_type!r}"
    )

    # The plan event should carry the rule key and work_type
    plan_events = _events_of_type(pool, impl_issue.id, "plan")
    assert len(plan_events) == 1
    payload = plan_events[0].payload
    assert rule_key in payload.get("rules", []), (
        f"rule key {rule_key!r} not in plan payload rules: {payload.get('rules')}"
    )
    assert payload.get("work_type") == "new-endpoint"


# --------------------------------------------------------------------------- #
# 9. injection content — spy reasoner captures rules param — DB-backed
# --------------------------------------------------------------------------- #

class _SpyReasoner(StubReasoner):
    """Captures the rules= string passed to plan_issue."""
    captured_rules: list[str]

    def __init__(self):
        self.captured_rules = []

    def plan_issue(self, issue: Issue, rules: str = "") -> str:
        self.captured_rules.append(rules)
        return super().plan_issue(issue, rules=rules)


def test_injection_content_matching(settings, pool):
    # Rule scoped to backend/api-repo
    rule = repo.create_adr(pool, "api", "Backend rule",
                           decision="Log every request.",
                           status="accepted",
                           applies_to={"repos": ["api-repo"]})
    rule_key = rule["adr_key"]

    goal = repo.create_goal(pool, "Implement feature", "Backend work.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    spy = _SpyReasoner()
    engine = _make_engine(settings, pool, reasoner=spy)
    engine.run()

    # At least one captured rules block should contain the rule key
    matching = [r for r in spy.captured_rules if rule_key in r]
    assert matching, (
        f"No captured rules block contained {rule_key!r}. "
        f"Captured: {spy.captured_rules!r}"
    )
    assert "Log every request." in matching[0]


def test_injection_content_non_matching(settings, pool):
    # Rule scoped to web-repo only; backend issue should NOT receive it
    rule = repo.create_adr(pool, "api", "Frontend-only rule",
                           decision="Use Tailwind.",
                           status="accepted",
                           applies_to={"repos": ["web-repo"]})
    rule_key = rule["adr_key"]

    goal = repo.create_goal(pool, "Implement backend service", "Backend work.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    spy = _SpyReasoner()
    engine = _make_engine(settings, pool, reasoner=spy)
    engine.run()

    # No captured block should contain the frontend-only rule key
    matching = [r for r in spy.captured_rules if rule_key in r]
    assert not matching, (
        f"Backend issue received frontend-only rule {rule_key!r}. "
        f"Captured: {spy.captured_rules!r}"
    )


# --------------------------------------------------------------------------- #
# 10. old-signature reasoner (TypeError fallback) — DB-backed
# --------------------------------------------------------------------------- #

class _OldSignatureReasoner:
    """Mimics a pre-rules reasoner: no rules param on plan_issue or gate_review."""

    def decompose_goal(self, goal: Goal, max_subissues: int, rules: str = "", sizing: str = "") -> list[IssueSpec]:
        return [
            IssueSpec(title=f"Implement: {goal.title}"),
            IssueSpec(title=f"Test: {goal.title}"),
        ][:max_subissues]

    def plan_issue(self, issue: Issue) -> str:
        return f"Old-style plan for {issue.title}"

    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list] = None) -> GateReview:
        return GateReview(passed=True, reasons=["old-style pass"])

    def score_drift(self, issue, recent=None) -> float:
        return 1.0


def test_old_signature_reasoner_completes(settings, pool):
    goal = repo.create_goal(pool, "Old-sig goal", "Should still complete.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=_OldSignatureReasoner())
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    assert all(i.state == "done" for i in issues), (
        f"Expected all done; states={[i.state for i in issues]}"
    )


# --------------------------------------------------------------------------- #
# 11. violated_rules recorded — DB-backed
# --------------------------------------------------------------------------- #

class _ViolatingReasoner(StubReasoner):
    """Declines qa_gate once with violated_rules, then always passes."""

    def __init__(self):
        self._declined: set[int] = set()

    def gate_review(self, issue, gate_type, recent=None, rules: str = "") -> GateReview:
        if gate_type == "qa_gate" and issue.id not in self._declined:
            self._declined.add(issue.id)
            return GateReview(passed=False, reasons=["rule violated"],
                              violated_rules=["ADR-API-001"])
        return GateReview(passed=True,
                          reasons=[f"{gate_type} exit criteria met (stub)"])


def test_violated_rules_recorded(settings, pool):
    goal = repo.create_goal(pool, "Violated rules goal", "Test violated_rules.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    engine = _make_engine(settings, pool, reasoner=_ViolatingReasoner(),
                          retry_cap=5)
    engine.run()

    issues = repo.list_issues(pool, goal_id=goal.id)
    # All issues should eventually complete (violating reasoner passes on retry)
    assert all(i.state == "done" for i in issues), (
        f"Expected all done; states={[i.state for i in issues]}"
    )

    # Find at least one gate_decline event with violated_rules == ['ADR-API-001']
    found_violated = False
    for issue in issues:
        for event in repo.recent_events(pool, issue.id, limit=200):
            vr = event.payload.get("violated_rules")
            if vr == ["ADR-API-001"]:
                found_violated = True
                break
        if found_violated:
            break

    assert found_violated, (
        "Expected at least one event with violated_rules=['ADR-API-001']"
    )


# --------------------------------------------------------------------------- #
# 12. gap proposal — DB-backed
# --------------------------------------------------------------------------- #

class _GappyReasoner(StubReasoner):
    """suggest_adr always returns a fixed draft."""

    def suggest_adr(self, issue) -> Optional[dict[str, Any]]:
        return {
            "domain": "docs",
            "title": "T",
            "decision": "D.",
            "context": "why",
            "applies_to": {"work_types": ["general"], "teams": [], "repos": []},
        }


def test_gap_proposal(settings, pool):
    # Empty ADRs table — no governing rules
    goal = repo.create_goal(pool, "General task", "No rules govern this.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    reasoner = _GappyReasoner()
    engine = _make_engine(settings, pool, reasoner=reasoner)
    summaries = engine.run()

    # At least one proposed ADR should exist
    proposed = repo.list_adrs(pool, status="proposed")
    assert len(proposed) >= 1, "Expected at least one proposed ADR after gap detection"

    # proposed_by should start with 'agent:'
    assert any(a["proposed_by"].startswith("agent:") for a in proposed), (
        f"No ADR with proposed_by starting 'agent:'; got "
        f"{[a['proposed_by'] for a in proposed]}"
    )

    # An 'adr_proposed' event should exist somewhere
    all_issues = repo.list_issues(pool, goal_id=goal.id)
    adr_proposed_events = []
    for issue in all_issues:
        adr_proposed_events.extend(_events_of_type(pool, issue.id, "adr_proposed"))
    assert len(adr_proposed_events) >= 1, "Expected at least one 'adr_proposed' event"

    # TickSummary.adr_proposals total should be >= 1
    total_proposals = sum(s.adr_proposals for s in summaries)
    assert total_proposals >= 1, (
        f"Expected adr_proposals >= 1, got {total_proposals}"
    )


# --------------------------------------------------------------------------- #
# 13. no gap proposal when governed — DB-backed
# --------------------------------------------------------------------------- #

def test_no_gap_proposal_when_governed(settings, pool):
    # Create an accepted project-wide rule (empty applies_to → matches everything)
    repo.create_adr(pool, "api", "Project-wide rule",
                    decision="Always add tests.",
                    status="accepted",
                    applies_to={})

    goal = repo.create_goal(pool, "General governed task", "Covered by a rule.")
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")

    reasoner = _GappyReasoner()
    engine = _make_engine(settings, pool, reasoner=reasoner)
    engine.run()

    # No proposed ADRs should have been created
    proposed = repo.list_adrs(pool, status="proposed")
    assert len(proposed) == 0, (
        f"Expected zero proposed ADRs when governed; got {proposed}"
    )


# --------------------------------------------------------------------------- #
# 14. dashboard ADR routes — DB-backed
# --------------------------------------------------------------------------- #

def test_dashboard_adrs(settings, pool):
    # Seed a proposed and an accepted ADR with a cross-link
    accepted = repo.create_adr(pool, "api", "Accepted rule",
                                decision="Use bearer tokens.",
                                status="accepted",
                                applies_to={})
    accepted_key = accepted["adr_key"]

    proposed = repo.create_adr(pool, "api", "Proposed rule",
                                decision="Add rate limiting.",
                                status="proposed",
                                related=[accepted_key])
    proposed_key = proposed["adr_key"]

    client = TestClient(create_app(pool, settings))

    # GET /adrs — 200, contains both keys and the Approve button
    resp = client.get("/adrs")
    assert resp.status_code == 200
    assert accepted_key in resp.text
    assert proposed_key in resp.text
    assert "Approve" in resp.text

    # GET /adrs/{accepted_key} — 200, contains decision text
    resp = client.get(f"/adrs/{accepted_key}")
    assert resp.status_code == 200
    assert "Use bearer tokens." in resp.text

    # GET /adrs/{proposed_key} — 200, contains decision and backlink to accepted
    # (proposed has related=[accepted_key], so accepted has 'linked from' proposed)
    resp = client.get(f"/adrs/{proposed_key}")
    assert resp.status_code == 200
    assert "Add rate limiting." in resp.text

    # The accepted ADR detail page should show the proposed ADR as a backlink
    # ("linked from") because the proposed ADR has related=[accepted_key]
    resp = client.get(f"/adrs/{accepted_key}")
    assert resp.status_code == 200
    assert "linked from" in resp.text
    assert proposed_key in resp.text

    # GET /adrs/{unknown_key} — 404
    resp = client.get("/adrs/ADR-NONE-999")
    assert resp.status_code == 404

    # POST /adrs/{proposed_key}/approve — 303, then rule becomes accepted
    resp = client.post(f"/adrs/{proposed_key}/approve", follow_redirects=False)
    assert resp.status_code == 303

    updated = repo.get_adr(pool, proposed_key)
    assert updated["status"] == "accepted"
