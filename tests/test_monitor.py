"""Orchestration-monitor queue: engine skip, repo/MCP helpers, dashboard page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.dashboard.app import create_app
from orchestrator.engine.loop import Engine, TickSummary
from orchestrator.mcp_server import tools_skills


class _DraftReasoner(StubReasoner):
    """Deterministic suggested reply for dashboard tests."""
    def draft_reply(self, message, context=""):
        return f"DRAFT-ANSWER for {message['subject']}"


class _Recorder:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


# --- engine: monitor messages stay pending, real-team messages still ingest -- #

def test_orchestration_message_stays_pending(settings, pool):
    eng = Engine(settings, pool, reasoner=StubReasoner())  # stub triage accepts all
    repo.create_message(pool, from_team="backend", to_team="orchestration",
                        subject="How does X work?", body="?")
    eng.tick()
    pend = repo.pending_messages(pool, to_team="orchestration")
    assert len(pend) == 1 and pend[0]["status"] == "pending"  # not ingested/rejected
    assert repo.list_issues(pool) == []  # no worker issue minted


def test_alias_arch_also_stays_pending(settings, pool):
    eng = Engine(settings, pool, reasoner=StubReasoner())
    repo.create_message(pool, from_team="backend", to_team="orch-monitor", subject="Q", body="?")
    eng.tick()
    # resolved to the orchestration team -> not rejected, still pending
    assert repo.pending_messages(pool, to_team="orch-monitor")[0]["status"] == "pending"


def test_real_team_message_still_ingests(settings, pool):
    eng = Engine(settings, pool, reasoner=StubReasoner())
    repo.create_message(pool, from_team="frontend", to_team="backend",
                        subject="Need an endpoint", body="please")
    eng.tick()
    assert repo.pending_messages(pool, to_team="backend") == []  # consumed
    assert any(i.team == "backend" for i in repo.list_issues(pool))  # issue created


# --- repository: respond / draft / responses ------------------------------- #

def test_respond_to_message_sends_archives_and_logs(settings, pool):
    goal = repo.create_goal(pool, "G")
    issue = repo.create_issue(pool, goal.id, "blocked work")
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="schema?", body="what shape", issue_id=issue.id)
    resp = repo.respond_to_message(pool, q["id"], "Use a flat array keyed method+path.")
    assert resp["kind"] == "response" and resp["to_team"] == "backend"
    assert resp["status"] == "sent" and resp["issue_id"] == issue.id
    assert repo.get_message(pool, q["id"])["status"] == "archived"  # left the queue
    # answer dropped into the issue timeline for a resuming worker
    evs = [e.event_type for e in repo.recent_events(pool, issue.id, limit=20)]
    assert "comms_response_received" in evs
    # readable via the response inbox
    inbox = repo.list_responses(pool, to_team="backend")
    assert inbox and inbox[0]["body"].startswith("Use a flat array")


def test_set_message_draft(settings, pool):
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="q", body="b")
    repo.set_message_draft(pool, q["id"], "cached draft")
    assert repo.get_message(pool, q["id"])["draft_response"] == "cached draft"


# --- MCP: comms_read -------------------------------------------------------- #

def test_comms_read_returns_responses(settings, pool):
    rec = _Recorder()
    tools_skills.register(rec, pool)
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="q", body="b")
    repo.respond_to_message(pool, q["id"], "the answer")
    got = rec.tools["comms_read"](team="backend")
    assert got and got[0]["body"] == "the answer" and got[0]["kind"] == "response"
    # comms_check (requests) must NOT return the response
    assert rec.tools["comms_check"](team="backend") == []


# --- dashboard: page + submit ---------------------------------------------- #

def _client(pool, settings):
    return TestClient(create_app(pool, settings, reasoner=_DraftReasoner()))


def test_monitor_page_lists_and_caches_draft(settings, pool):
    q = repo.create_message(pool, from_team="backend", to_team="orch-monitor",
                            subject="ingestion shape?", body="need it")
    client = _client(pool, settings)
    html = client.get("/orch/monitor").text
    assert "ingestion shape?" in html
    assert "DRAFT-ANSWER for ingestion shape?" in html
    # draft is the QA'd (2-pass) output and is cached so a reload won't re-call.
    cached = repo.get_message(pool, q["id"])["draft_response"]
    assert "DRAFT-ANSWER for ingestion shape?" in cached and cached.startswith("[qa]")


def test_monitor_submit_uses_override(settings, pool):
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="q", body="b")
    client = _client(pool, settings)
    r = client.post(f"/orch/monitor/{q['id']}/respond",
                    data={"suggested": "draft text", "override": "  human answer  "},
                    follow_redirects=False)
    assert r.status_code == 303
    inbox = repo.list_responses(pool, to_team="backend")
    assert inbox[0]["body"] == "human answer"  # override wins, trimmed


def test_monitor_history_panel_shows_correspondence(settings, pool):
    # a question + its sent response should both appear in the history side panel
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="history-q", body="b", issue_id=None)
    repo.respond_to_message(pool, q["id"], "history-answer")
    client = _client(pool, settings)
    html = client.get("/orch/monitor").text
    assert "Correspondence" in html              # the side panel
    assert "history-q" in html                   # the question shows in history
    assert "Re: history-q" in html               # and its response


def test_fleet_index_shows_open_message_badge(settings, pool):
    repo.create_message(pool, from_team="backend", to_team="orch-monitor",
                        subject="needs review", body="?")
    client = _client(pool, settings)
    html = client.get("/").text
    assert "open message(s) in the orchestrator queue" in html  # alert badge/banner
    assert "Correspondence" in html                             # fleet side panel


def test_monitor_submit_falls_back_to_suggested(settings, pool):
    q = repo.create_message(pool, from_team="backend", to_team="orchestration",
                            subject="q", body="b")
    client = _client(pool, settings)
    client.post(f"/orch/monitor/{q['id']}/respond",
                data={"suggested": "the draft", "override": ""},
                follow_redirects=False)
    inbox = repo.list_responses(pool, to_team="backend")
    assert inbox[0]["body"] == "the draft"  # no override -> draft sent
    assert repo.get_message(pool, q["id"])["status"] == "archived"
