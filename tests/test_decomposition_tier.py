"""Decomposition-tier wiring: config layering (tier presets under explicit
overrides), the reasoner sizing clause, and tier-aware agent-doc rendering.

The pure tier presets themselves live in test_decomposition.py; this module covers
how a bootstrapped tier flows into Settings, the decompose prompt, and worker docs.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator import agent_docs
from orchestrator import config
from orchestrator import decomposition as dec
from orchestrator.agents.reasoning import _LLMReasoner
from orchestrator.config import load_settings
from orchestrator.models import Goal


_THRESHOLD_ENVS = [
    "DECOMPOSITION_TIER", "MAX_SUBISSUES", "MAX_CHILDREN_PER_PARENT",
    "MAX_ISSUES_PER_GOAL", "MAX_DEPTH", "RETRY_CAP", "DRIFT_THRESHOLD",
    "ORCH_INSTANCE", "ROSTER_FILE",
]


def _clean_env(monkeypatch):
    monkeypatch.setattr(config, "_yaml", lambda _path: {})
    for name in _THRESHOLD_ENVS:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Config: tier selection + threshold layering
# --------------------------------------------------------------------------- #

def test_default_tier_is_mid_with_mid_caps(monkeypatch):
    _clean_env(monkeypatch)
    s = load_settings()
    assert s.decomposition_tier == dec.MID
    assert s.thresholds.max_subissues == 8
    assert s.thresholds.max_children_per_parent == 5


def test_high_tier_env_applies_coarser_caps(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DECOMPOSITION_TIER", "high")
    s = load_settings()
    assert s.decomposition_tier == dec.HIGH
    assert s.thresholds.max_subissues == 6
    assert s.thresholds.max_children_per_parent == 4


def test_remedial_tier_env_applies_finer_caps_and_extra_retry(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DECOMPOSITION_TIER", "remedial")
    s = load_settings()
    assert s.decomposition_tier == dec.REMEDIAL
    assert s.thresholds.max_subissues == 10
    assert s.thresholds.max_issues_per_goal == 40
    assert s.thresholds.retry_cap == 4
    # Quarantine threshold is NOT tightened — remedial advises, it does not halt more.
    assert s.thresholds.drift_threshold == 0.5


def test_unknown_tier_env_degrades_to_mid(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DECOMPOSITION_TIER", "banana")
    s = load_settings()
    assert s.decomposition_tier == dec.MID
    assert s.thresholds.max_subissues == 8


def test_explicit_threshold_env_wins_over_tier_preset(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DECOMPOSITION_TIER", "high")   # tier default would be 6
    monkeypatch.setenv("MAX_SUBISSUES", "99")          # explicit override
    s = load_settings()
    assert s.decomposition_tier == dec.HIGH
    assert s.thresholds.max_subissues == 99            # explicit beats tier


def test_explicit_yaml_thresholds_win_over_tier_preset(monkeypatch):
    # A project that pins its own thresholds keeps them even at a non-default tier.
    def fake_yaml(path: Path):
        if str(path).endswith("settings.yaml"):
            return {"decomposition_tier": "high", "thresholds": {"max_subissues": 3}}
        return {}
    monkeypatch.setattr(config, "_yaml", fake_yaml)
    for name in _THRESHOLD_ENVS:
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert s.decomposition_tier == dec.HIGH
    assert s.thresholds.max_subissues == 3             # pinned yaml beats tier's 6


def test_per_instance_tier_from_settings_block(monkeypatch):
    # The instance `settings:` block is merged into s_yaml, so a per-project
    # decomposition_tier there is honored.
    def fake_yaml(path: Path):
        if str(path).endswith("instances.yaml"):
            return {"instances": {"proj": {
                "database_url": "postgresql://x@localhost/proj",
                "settings": {"decomposition_tier": "remedial"}}}}
        return {}
    monkeypatch.setattr(config, "_yaml", fake_yaml)
    for name in _THRESHOLD_ENVS:
        monkeypatch.delenv(name, raising=False)
    s = load_settings(instance="proj")
    assert s.decomposition_tier == dec.REMEDIAL
    assert s.thresholds.max_subissues == 10


# --------------------------------------------------------------------------- #
# Reasoner: the sizing clause reaches the decompose prompt
# --------------------------------------------------------------------------- #

class _CapturingLLM(_LLMReasoner):
    """Captures the system prompt instead of hitting a model."""
    def __init__(self):
        self.system = ""

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        self.system = system
        return "[]"   # empty decomposition; we only inspect the prompt


def _goal():
    return Goal(id=1, title="Build a thing", description="do it")


def test_sizing_defaults_to_one_deliverable_when_absent():
    r = _CapturingLLM()
    r.decompose_goal(_goal(), max_subissues=5)
    assert "ONE deliverable" in r.system


def test_tier_sizing_flows_into_prompt():
    r = _CapturingLLM()
    r.decompose_goal(_goal(), max_subissues=5, sizing=dec.resolve_tier("high").sizing)
    assert "cohesive" in r.system.lower()
    assert "internally parallel" in r.system.lower()


# --------------------------------------------------------------------------- #
# Agent docs: tier flags shape the rendered worker instructions
# --------------------------------------------------------------------------- #

def _doc(function="dev", internal_parallelism=False, midrun_checks=False):
    return agent_docs.render_agent_doc(
        vendor="claude", team="backend", function=function, agent_id=1, rules=[],
        internal_parallelism=internal_parallelism, midrun_checks=midrun_checks)


def test_mid_tier_dev_doc_bans_per_issue_subagents():
    doc = _doc()
    assert "do NOT spawn parallel subagents on a single issue" in doc
    assert "Mid-run checks" not in doc


def test_high_tier_dev_doc_permits_internal_parallelism():
    doc = _doc(internal_parallelism=True)
    assert "parallelize WITHIN a single issue" in doc
    assert "high** decomposition tier" in doc


def test_high_tier_parallelism_is_dev_only():
    # QA at the high tier must NOT get the per-issue fan-out license.
    qa = _doc(function="qa", internal_parallelism=True)
    assert "parallelize WITHIN a single issue" not in qa
    assert "do NOT spawn parallel subagents on a single issue" in qa


def test_remedial_tier_doc_adds_midrun_advisory_note():
    doc = _doc(midrun_checks=True)
    assert "Mid-run checks (remedial tier)" in doc
    assert "NOT a stop order" in doc
