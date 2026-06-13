"""my_queue: unified work + unread inbound messages, read/unread, thread links."""

from __future__ import annotations

from orchestrator import repository as repo
from orchestrator.mcp_server import tools_issues


class _Recorder:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(pool):
    rec = _Recorder()
    tools_issues.register(rec, pool)
    return rec.tools


def _agent_with_work(pool):
    agent = repo.register_agent(pool, "backend", "dev", "external")
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    issue = repo.create_issue(pool, goal.id, "do the thing", pipeline="pull-1", team="backend")
    repo.claim_issue(pool, issue.id, agent.id)
    repo.update_state(pool, issue.id, "in_progress", gate_type="implementation")
    return agent, issue


def test_my_queue_unifies_work_and_answers_with_links(pool):
    tools = _tools(pool)
    agent, issue = _agent_with_work(pool)
    # backend asked a question (linked to its issue); orch-monitor answered it.
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="schema?", body="what shape", issue_id=issue.id)
    repo.respond_to_message(pool, q["id"], "flat array keyed (method,path)")

    queue = tools["my_queue"](agent.id)
    assert [w["id"] for w in queue["work"]] == [issue.id]            # work item
    answers = [m for m in queue["messages"] if m["type"] == "answer"]
    assert len(answers) == 1
    a = answers[0]
    assert a["source"] == "orchestration"          # who answered
    assert a["issue_id"] == issue.id               # linked to the originating issue
    assert a["reply_to"] == q["id"]                # thread link back to the request
    assert a["body"].startswith("flat array")


def test_mark_read_drops_message_from_queue(pool):
    tools = _tools(pool)
    agent, issue = _agent_with_work(pool)
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="q", body="b", issue_id=issue.id)
    resp = repo.respond_to_message(pool, q["id"], "the answer")

    before = tools["my_queue"](agent.id)["messages"]
    assert any(m["id"] == resp["id"] for m in before)
    assert tools["mark_read"](resp["id"]) == {"status": "ok"}
    after = tools["my_queue"](agent.id)["messages"]
    assert not any(m["id"] == resp["id"] for m in after)   # consumed -> gone


def test_my_queue_includes_pending_requests(pool):
    tools = _tools(pool)
    agent, _ = _agent_with_work(pool)
    repo.create_message(pool, from_team="frontend", to_team="backend",
                        subject="please add X", body="...")
    reqs = [m for m in tools["my_queue"](agent.id)["messages"] if m["type"] == "request"]
    assert len(reqs) == 1 and reqs[0]["source"] == "frontend"


def test_list_my_work_unchanged(pool):
    # The single-purpose work list still returns a flat list of issue dicts.
    tools = _tools(pool)
    agent, issue = _agent_with_work(pool)
    mine = tools["list_my_work"](agent.id)
    assert isinstance(mine, list) and [i["id"] for i in mine] == [issue.id]
