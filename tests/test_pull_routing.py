"""Engine routing for the pull model (pull-1 pipeline).

Pull gates (implementation, verification) are owned by registered `external`
workers: the engine assigns + observes but never runs a worker on them. The
external worker is simulated here via repository calls (the hermetic stand-in for
a live Claude Code / Codex / Aider instance) — it reports evidence and advances
the gate exactly as the MCP `gate_decision` tool would.
"""

from __future__ import annotations

import copy

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.engine.loop import Engine
from orchestrator.state_machine import apply_gate_decision


def _engine(settings, pool):
    return Engine(copy.deepcopy(settings), pool, reasoner=StubReasoner())


def _event_types(pool, issue_id):
    return [e.event_type for e in repo.recent_events(pool, issue_id, limit=200)]


def _worker_completes_gate(engine, pool, issue_id, *, passed=True):
    """Mirror the MCP gate_decision tool: an external worker finished its repo
    work and advances the pull gate. Identical to mcp_server.tools_issues."""
    issue = repo.get_issue(pool, issue_id)
    pipeline = engine.pipelines[issue.pipeline]
    gate = pipeline.gate(issue.gate_type)
    outcome = apply_gate_decision(
        pipeline, gate, passed=passed, retry_count=issue.retry_count,
        retry_cap=engine.t.retry_cap, triggered_by_message=issue.triggered_by_message,
    )
    repo.update_state(pool, issue_id, outcome.state, gate_type=outcome.gate_type,
                      event_type=outcome.event_type, retry_count=outcome.retry_count)


def _setup_pull_issue(pool):
    goal = repo.create_goal(pool, "Add endpoint", "via pull agents", pipeline="pull-1")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "Implement /health",
                              pipeline="pull-1", team="backend")
    lead = repo.register_agent(pool, "backend", "lead", "api")
    dev = repo.register_agent(pool, "backend", "dev", "external")
    qa = repo.register_agent(pool, "backend", "qa", "external")
    return goal, issue, lead, dev, qa


def test_engine_parks_pull_gate_on_external_worker_without_generating_code(settings, pool):
    _, issue, lead, dev, qa = _setup_pull_issue(pool)
    eng = _engine(settings, pool)

    eng.run(max_ticks=20)

    parked = repo.get_issue(pool, issue.id)
    assert parked.state == "in_progress"
    assert parked.gate_type == "implementation"          # advanced past intake (verdict)
    assert parked.assigned_agent == dev.id               # owned by the external dev worker
    # the engine NEVER ran a worker on the pull gate
    assert "code_generated" not in _event_types(pool, issue.id)


def test_pull_handoff_dev_to_qa_and_completion(settings, pool):
    _, issue, lead, dev, qa = _setup_pull_issue(pool)
    eng = _engine(settings, pool)

    eng.run(max_ticks=20)  # parks at implementation (external dev)
    assert repo.get_issue(pool, issue.id).assigned_agent == dev.id

    # dev coder reports its commit and completes the implementation pull gate
    repo.append_log(pool, issue.id, "code_committed",
                    {"sha": "abc123", "tests_passed": True})
    _worker_completes_gate(eng, pool, issue.id)

    eng.run(max_ticks=20)  # re-assigns to the external QA runner at verification
    at_verify = repo.get_issue(pool, issue.id)
    assert at_verify.gate_type == "verification"
    assert at_verify.assigned_agent == qa.id

    # QA runner reports results and completes verification → verdict gates follow
    repo.append_log(pool, issue.id, "tests_run",
                    {"passed": 42, "failures": 0, "summary": "ok"})
    _worker_completes_gate(eng, pool, issue.id)

    eng.run(max_ticks=20)  # reasoner renders the qa_gate/completion verdicts
    done = repo.get_issue(pool, issue.id)
    assert done.state == "done"
    assert "code_generated" not in _event_types(pool, issue.id)


def test_pull_gate_waits_when_no_external_worker_registered(settings, pool):
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "i", pipeline="pull-1", team="backend")
    repo.register_agent(pool, "backend", "lead", "api")   # only the verdict role
    eng = _engine(settings, pool)

    eng.run(max_ticks=20)

    # No external dev worker → the issue parks at the pull gate, unworked.
    stuck = repo.get_issue(pool, issue.id)
    assert stuck.gate_type == "implementation"
    assert stuck.state == "in_progress"
    # not owned by the api lead agent (pull gates require an external worker)
    owner = repo.get_agent(pool, stuck.assigned_agent) if stuck.assigned_agent else None
    assert owner is None or owner.runtime == "external"
    assert "code_generated" not in _event_types(pool, issue.id)
