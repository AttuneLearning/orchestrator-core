"""Repository tests against in-session Postgres."""

from orchestrator import repository as repo


def test_create_goal_and_issue(pool):
    goal = repo.create_goal(pool, "Build feature X", "details")
    assert goal.id > 0
    assert goal.state == "backlog"

    issue = repo.create_issue(pool, goal.id, "Implement endpoint", team="backend")
    assert issue.goal_id == goal.id
    assert issue.depth == 0
    assert issue.state == "backlog"

    # creation event was logged
    events = repo.recent_events(pool, issue.id)
    assert any(e.event_type == "created" for e in events)


def test_subissue_inherits_goal_and_depth(pool):
    goal = repo.create_goal(pool, "Goal")
    parent = repo.create_issue(pool, goal.id, "Parent")
    child = repo.create_subissue(pool, parent, "Child")
    assert child.parent_id == parent.id
    assert child.goal_id == parent.goal_id
    assert child.depth == parent.depth + 1


def test_update_state_writes_matching_event(pool):
    goal = repo.create_goal(pool, "Goal")
    issue = repo.create_issue(pool, goal.id, "I")

    updated = repo.update_state(
        pool, issue.id, "in_review", gate_type="intake", event_type="gate_enter"
    )
    assert updated.state == "in_review"
    assert updated.gate_type == "intake"

    events = repo.recent_events(pool, issue.id)
    latest = events[0]  # ordered seq DESC
    assert latest.event_type == "gate_enter"
    assert latest.from_state == "backlog"
    assert latest.to_state == "in_review"
    # seq is monotonic per issue
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs, reverse=True)


def test_count_issues_for_goal(pool):
    goal = repo.create_goal(pool, "Goal")
    for i in range(3):
        repo.create_issue(pool, goal.id, f"I{i}")
    assert repo.count_issues_for_goal(pool, goal.id) == 3


def test_agent_register_and_find_idle(pool):
    a = repo.register_agent(pool, "backend", "dev", "api")
    assert a.status == "idle"
    found = repo.find_idle_agent(pool, "backend", "dev")
    assert found is not None and found.id == a.id


def test_claim_marks_agent_busy(pool):
    goal = repo.create_goal(pool, "Goal")
    issue = repo.create_issue(pool, goal.id, "I")
    agent = repo.register_agent(pool, "backend", "dev")
    repo.claim_issue(pool, issue.id, agent.id)

    refreshed = repo.get_issue(pool, issue.id)
    assert refreshed.assigned_agent == agent.id
    agents = repo.list_agents(pool, "backend")
    assert agents[0].status == "busy"


def test_memory_write_recall_search(pool):
    repo.memory_write(pool, "Postgres is canonical", scope="global")
    repo.memory_write(pool, "agent note", scope="agent:7")
    assert len(repo.memory_recall(pool, "global")) == 1
    hits = repo.memory_search(pool, "canonical")
    assert len(hits) == 1 and "canonical" in hits[0].body


def test_adr_key_increments_per_domain(pool):
    a1 = repo.create_adr(pool, "data", "Use Postgres")
    a2 = repo.create_adr(pool, "data", "Use pgvector")
    assert a1["adr_key"] == "ADR-DATA-001"
    assert a2["adr_key"] == "ADR-DATA-002"
