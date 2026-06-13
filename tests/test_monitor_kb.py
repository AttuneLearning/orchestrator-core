"""Orchestration-monitor knowledge base: build, isolation, grounded drafting,
and the periodic git-review alert."""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from orchestrator import monitor_kb, repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.dashboard.app import create_app


# --- KB build + isolation --------------------------------------------------- #

def test_build_monitor_kb_populates_isolated_scope(settings, pool):
    n = monitor_kb.build_monitor_kb(pool, settings)
    assert n > 0
    notes = repo.memory_recall(pool, "monitor:kb", limit=200)
    assert len(notes) == n
    bodies = "\n".join(x.body for x in notes)
    assert "[contract-store]" in bodies            # the authored contract spec
    assert any(x.body.startswith("[repo:") for x in notes)  # per-fn repo signatures
    assert any("[schema:" in x.body for x in notes)  # migration DDL summaries


def test_kb_carries_precise_contract_facts(settings, pool):
    monitor_kb.build_monitor_kb(pool, settings)
    notes = {n.body for n in repo.memory_recall(pool, "monitor:kb", limit=500)}
    blob = "\n".join(notes)
    # exact upsert_contract signature (defaults -> required vs optional)
    assert any(b.startswith("[repo:upsert_contract]") and "request_ref" in b for b in notes)
    # real DDL with NOT NULL/DEFAULT clauses ingested (not just the comment block)
    assert any("[schema:0011" in b and "DEFAULT" in b for b in notes)
    # the authored contract note is now precise on the 3 previously-wrong points
    spec = next(b for b in notes if b.startswith("[contract-store]"))
    assert "REQUIRED fields: method, path" in spec
    assert "proposed | agreed | live | deprecated" in spec
    assert "sha256 of method|path|request_ref|response_dto" in spec


def test_build_is_idempotent(settings, pool):
    a = monitor_kb.build_monitor_kb(pool, settings)
    b = monitor_kb.build_monitor_kb(pool, settings)  # wipe + rebuild
    assert a == b
    assert len(repo.memory_recall(pool, "monitor:kb", limit=500)) == b  # no doubling


def test_monitor_scope_is_private_from_default_search(settings, pool):
    monitor_kb.build_monitor_kb(pool, settings)
    repo.memory_write(pool, "an agent's own note about contracts", scope="agent:1")
    # default (no scope) search excludes monitor:* entirely
    hits = repo.memory_search(pool, "contract", limit=50)
    assert hits and all(not n.scope.startswith("monitor:") for n in hits)
    # an agent scope sees only its own
    agent_hits = repo.memory_search(pool, "contract", limit=50, scope="agent:1")
    assert all(n.scope == "agent:1" for n in agent_hits)
    # the monitor's own scoped search sees only monitor:kb
    kb_hits = repo.memory_search(pool, "contract", limit=50, scope="monitor:kb")
    assert kb_hits and all(n.scope == "monitor:kb" for n in kb_hits)


def test_retrieve_context_surfaces_the_right_notes(settings, pool):
    monitor_kb.build_monitor_kb(pool, settings)
    ctx = monitor_kb.retrieve_context(
        pool, "MCP contract store ingestion schema for contracts.seed.json", limit=8)
    assert ctx, "expected keyword-overlap hits"
    # the authoritative contract spec surfaces at the top (it has the precise
    # required/optional + status-enum + hash facts the draft needs)
    assert ctx[0].startswith("[contract-store]")
    # an unrelated query doesn't pull the contract spec to the top
    other = monitor_kb.retrieve_context(pool, "agent heartbeat liveness reclaim", limit=3)
    assert not other or not other[0].startswith("[contract-store]")


def test_bootstrap_only_when_empty(settings, pool):
    assert monitor_kb.monitor_kb_empty(pool) is True
    first = monitor_kb.bootstrap_monitor_kb(pool, settings)
    assert first > 0
    assert monitor_kb.bootstrap_monitor_kb(pool, settings) == 0  # already built


# --- grounded drafting ------------------------------------------------------ #

def test_dashboard_draft_is_grounded_in_kb(settings, pool):
    monitor_kb.build_monitor_kb(pool, settings)
    repo.create_message(pool, from_team="backend", to_team="orch-monitor",
                        subject="contract store ingestion schema?",
                        body="what shape does import-contracts expect")
    # StubReasoner.draft_reply tags the output [draft+ctx] when context is supplied,
    # proving the dashboard retrieved KB snippets and passed them in.
    client = TestClient(create_app(pool, settings, reasoner=StubReasoner()))
    html = client.get("/orch/monitor").text
    # [draft+ctx] = grounded (pass 1); [qa] = cross-checked (pass 2). Both present
    # proves the draft was grounded AND ran through the QA cross-check.
    assert "[draft+ctx]" in html and "[qa]" in html


# --- git-review alert ------------------------------------------------------- #

def test_git_review_alerts_then_dedupes(settings, pool, monkeypatch):
    from orchestrator import cli

    state = {"head": "aaaa", "remote": "bbbb"}

    def fake_git(*a):
        out = ""
        if a[0] == "rev-parse":
            if "--abbrev-ref" in a:
                out = "orchestrator-core"
            elif a[-1] == "HEAD":
                out = state["head"]
            else:                       # origin/<branch>
                out = state["remote"]
        elif a[0] == "log":
            out = "bbbb fix: a thing"
        elif a[0] == "diff":
            out = "orchestrator/loop.py"
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(cli, "_git", fake_git)
    s = copy.deepcopy(settings)

    assert cli._cmd_git_review(None, s) == 0
    pend = repo.pending_messages(pool, to_team="orch-monitor")
    assert len(pend) == 1 and "Update available" in pend[0]["subject"]
    assert repo.get_system_state(pool, "last_reviewed_sha") == "bbbb"

    # second run, same remote SHA -> no duplicate alert
    assert cli._cmd_git_review(None, s) == 0
    assert len(repo.pending_messages(pool, to_team="orch-monitor")) == 1
