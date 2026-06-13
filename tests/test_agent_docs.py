"""Pure tests for SoT agent-doc rendering + drift detection (no DB)."""

from orchestrator import agent_docs

_RULES = [
    {"adr_key": "ADR-UI-001", "decision": "Follow Feature-Sliced Design layers."},
    {"adr_key": "ADR-DEV-001", "decision": "Ship a test with every change."},
]


def test_render_includes_identity_gate_header_and_rules():
    doc = agent_docs.render_agent_doc(vendor="claude", team="frontend",
                                      function="dev", agent_id=4, rules=_RULES)
    assert doc.startswith(agent_docs.GENERATED_HEADER)
    assert "agent_id: 4" in doc and "frontend" in doc
    assert "implementation" in doc                       # dev owns implementation
    assert "Claude Code loads this file" in doc          # claude bootstrap
    assert "ADR-UI-001" in doc and "Feature-Sliced Design" in doc  # rules embedded


def test_qa_and_lead_owned_gates():
    qa = agent_docs.render_agent_doc(vendor="claude", team="frontend", function="qa",
                                     agent_id=5, rules=[])
    assert "verification + e2e" in qa
    lead = agent_docs.render_agent_doc(vendor="claude", team="frontend", function="lead",
                                       agent_id=6, rules=[])
    assert "verdict gates only" in lead


def test_claude_and_codex_differ_only_in_bootstrap():
    cl = agent_docs.render_agent_doc(vendor="claude", team="backend", function="dev",
                                     agent_id=1, rules=_RULES)
    cx = agent_docs.render_agent_doc(vendor="codex", team="backend", function="dev",
                                     agent_id=1, rules=_RULES)
    assert agent_docs.filename_for("claude") == "CLAUDE.md"
    assert agent_docs.filename_for("codex") == "AGENTS.md"
    assert cl != cx
    # identical except the single bootstrap line
    diff = [(a, b) for a, b in zip(cl.splitlines(), cx.splitlines()) if a != b]
    assert len(diff) == 1 and "Claude Code" in diff[0][0] and "Codex" in diff[0][1]


def test_drift_detection():
    doc = agent_docs.render_agent_doc(vendor="claude", team="backend", function="dev",
                                      agent_id=1, rules=_RULES)
    assert agent_docs.drift(doc, None) is True                      # missing file
    assert agent_docs.drift(doc, doc) is False                      # identical
    assert agent_docs.drift(doc, doc + "\n   ") is False            # trailing ws tolerated
    assert agent_docs.drift(doc, doc.replace("agent_id: 1", "agent_id: 9")) is True
