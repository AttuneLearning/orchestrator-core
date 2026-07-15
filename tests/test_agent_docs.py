"""Pure tests for SoT agent-doc rendering + drift detection (no DB)."""

from types import SimpleNamespace

from orchestrator import agent_docs

_RULES = [
    {"adr_key": "ADR-UI-001", "decision": "Follow Feature-Sliced Design layers."},
    {"adr_key": "ADR-DEV-001", "decision": "Ship a test with every change."},
]


def _settings(workflow_profile="legacy", verify_worktrees=None):
    """A minimal Settings-like stand-in: only the attributes
    `orchestrator.workflow.loader.load_effective` and `_dev_sync_step` read."""
    return SimpleNamespace(
        workflow_profile=workflow_profile,
        verify_worktrees=verify_worktrees or {},
        workspace_manifest="",
    )


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


# --------------------------------------------------------------------------- #
# WP-20: the dev sync step's reconcile sentence, generated FROM the workflow
# profile when settings.workflow_profile == "enabled" AND a profile resolves;
# static (today's byte-identical text) otherwise. See agent_docs._dev_sync_step.
# --------------------------------------------------------------------------- #

def _dev_doc(**settings_kwargs):
    return agent_docs.render_agent_doc(vendor="claude", team="backend", function="dev",
                                       agent_id=1, rules=[], settings=_settings(**settings_kwargs))


def _no_settings_doc():
    return agent_docs.render_agent_doc(vendor="claude", team="backend", function="dev",
                                       agent_id=1, rules=[])


def test_legacy_output_byte_identical_to_static_default():
    """No settings at all (every existing caller/test) and an explicit `legacy`
    flag must both render byte-identical to today's static reconcile sentence —
    the drift check must stay green with zero regeneration on every instance."""
    baseline = _no_settings_doc()
    assert agent_docs._RECONCILE_STATIC in baseline

    legacy_doc = _dev_doc(workflow_profile="legacy")
    assert legacy_doc == baseline

    # Even a "legacy" flag with a worktree configured must not change anything.
    legacy_with_worktree_doc = _dev_doc(workflow_profile="legacy",
                                        verify_worktrees={"backend": "/nonexistent"})
    assert legacy_with_worktree_doc == baseline


def test_enabled_without_resolving_worktree_falls_back_to_static():
    """`enabled` but no `verify_worktrees` entry for the team -> static text."""
    doc = _dev_doc(workflow_profile="enabled", verify_worktrees={})
    assert doc == _no_settings_doc()
    assert agent_docs._RECONCILE_STATIC in doc


def test_enabled_profile_load_failure_falls_back_to_static(tmp_path, monkeypatch):
    """Any exception while loading/rendering the profile is swallowed -> static text
    (fail-safe hard rule: a broken profile must never wedge doc rendering)."""
    def _boom(*a, **kw):
        raise RuntimeError("simulated profile load failure")

    monkeypatch.setattr("orchestrator.workflow.loader.load_effective", _boom)
    doc = _dev_doc(workflow_profile="enabled", verify_worktrees={"backend": str(tmp_path)})
    assert doc == _no_settings_doc()
    assert agent_docs._RECONCILE_STATIC in doc


def test_enabled_with_profile_renders_sentence_from_prepare_action(tmp_path):
    """A single custom `prepare` action (repo layer) changes BOTH the trigger
    glob and the command in the rendered sentence."""
    orch_dir = tmp_path / ".orchestrator"
    orch_dir.mkdir()
    (orch_dir / "workflow.yaml").write_text(
        "prepare:\n"
        "  dev:\n"
        "    - run: \"npm ci --no-audit --no-fund\"\n"
        "      when_changed: [\"package-lock.json\"]\n"
    )
    doc = _dev_doc(workflow_profile="enabled", verify_worktrees={"backend": str(tmp_path)})
    assert doc != _no_settings_doc()
    assert "the merge changed `package-lock.json`" in doc
    assert "run `npm ci --no-audit --no-fund` before typecheck" in doc
    # the old static sentence's rationale text must be gone (fully replaced, not appended)
    assert agent_docs._RECONCILE_STATIC not in doc


def test_enabled_with_multiple_prepare_actions_renders_multiple_clauses(tmp_path):
    """Multiple `prepare` actions for role dev each render their own clause."""
    orch_dir = tmp_path / ".orchestrator"
    orch_dir.mkdir()
    (orch_dir / "workflow.yaml").write_text(
        "prepare:\n"
        "  dev:\n"
        "    - run: \"npm ci --no-audit --no-fund\"\n"
        "      when_changed: [\"package-lock.json\"]\n"
        "    - run: \"make generate\"\n"
        "      when_changed: [\"schema/*.proto\"]\n"
    )
    doc = _dev_doc(workflow_profile="enabled", verify_worktrees={"backend": str(tmp_path)})
    assert "run `npm ci --no-audit --no-fund` before typecheck" in doc
    assert "run `make generate` before typecheck" in doc
    assert "`schema/*.proto`" in doc


def test_enabled_stock_node_profile_builtin_matches_static_sentence(tmp_path):
    """The real node-deps-reconcile builtin (auto-detected via package-lock.json,
    no repo/workspace override) renders the SAME sentence as the static default —
    it's a named rendering of the identical real command + trigger glob."""
    (tmp_path / "package-lock.json").write_text("{}")
    doc = _dev_doc(workflow_profile="enabled", verify_worktrees={"backend": str(tmp_path)})
    assert agent_docs._RECONCILE_STATIC in doc
    assert doc == _no_settings_doc()
