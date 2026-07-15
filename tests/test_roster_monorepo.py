"""ROSTER_FILE selection + the monorepo roster variant."""

from __future__ import annotations

from orchestrator.config import load_settings
from orchestrator.roster import load_roster


def test_default_roster_is_independent(monkeypatch):
    # config/roster.yaml is the compiled default. We pin it explicitly because
    # an ambient ROSTER_FILE can still override YAML, so delenv alone can't
    # isolate the default.
    monkeypatch.setenv("ROSTER_FILE", "config/roster.yaml")
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
    # repos are monorepo package paths (ADR-scoping strings), not independent repos
    assert r.resolve("backend").repos == ("apps/api",)
    assert r.resolve("frontend").repos == ("apps/web",)
    # standard 7-agent lineup: be/fe/sr each dev+qa, plus the orch-manager lead
    assert r.resolve("be").id == "backend"
    assert r.resolve("fe").id == "frontend"
    assert r.resolve("sr").id == "senior"
    assert r.resolve("orch-monitor").id == "orchestration"
    assert {st.function for st in r.resolve("backend").sub_teams} == {"dev", "qa"}
    assert {st.function for st in r.resolve("senior").sub_teams} == {"dev", "qa"}
    assert {st.id for st in r.resolve("orchestration").sub_teams} == {"orch-manager"}
