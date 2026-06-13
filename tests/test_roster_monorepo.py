"""ROSTER_FILE selection + the monorepo roster variant."""

from __future__ import annotations

from orchestrator.config import load_settings
from orchestrator.roster import load_roster


def test_default_roster_is_independent(monkeypatch):
    monkeypatch.delenv("ROSTER_FILE", raising=False)
    s = load_settings()
    assert s.roster_file == "config/roster.yaml"
    r = load_roster(s.roster)
    assert r.resolve("backend").repos == ("api-repo",)
    assert r.resolve("frontend").repos == ("web-repo",)


def test_roster_file_selects_monorepo(monkeypatch):
    monkeypatch.setenv("ROSTER_FILE", "config/roster.monorepo.yaml")
    s = load_settings()
    assert s.roster_file == "config/roster.monorepo.yaml"
    r = load_roster(s.roster)
    # repos repointed to monorepo package paths (ADR-scoping strings)
    assert r.resolve("backend").repos == ("apps/api",)
    assert r.resolve("frontend").repos == ("apps/web",)
    assert r.resolve("contracts").repos == ("packages/contracts",)
    # aliases preserved across the variant
    assert r.resolve("contract").id == "contracts"
    assert r.resolve("orch-monitor").id == "orchestration"
    # same teams/pull model otherwise
    assert {st.function for st in r.resolve("backend").sub_teams} == {"dev", "qa", "lead"}
