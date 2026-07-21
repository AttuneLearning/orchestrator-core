"""8b: apply-time canonical-route gate on contract_lifecycle_preview/apply.

Opt-in (settings=None -> no-op), fail-safe (drift errors -> skip), and scoped to
contracts being ACTIVATED (agree/reinstate) — never gates supersede/retire.
"""
import pytest
from orchestrator import repository as repo
from orchestrator import contract_drift

PROJ = "cadencelms-working"


def _unbacked(cid, method="GET", path="/x"):
    return {"category": "unbacked_contract", "severity": "advisory",
            "method": method, "path": path, "contract_id": cid, "detail": "no route"}


def test_drift_for_contracts_failsafe(pool, settings, monkeypatch):
    def boom(pool, settings):
        raise RuntimeError("no product repo")
    monkeypatch.setattr(contract_drift, "run_drift_check", boom)
    assert contract_drift.drift_for_contracts(pool, settings, [1]) == \
        {"blocking": [], "advisory": [], "ok": False}


def test_apply_no_settings_skips_gate(pool, monkeypatch):
    c = repo.upsert_contract(pool, "GET", "/8b/nosettings", status="deprecated")
    monkeypatch.setattr(contract_drift, "drift_for_contracts",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate ran")))
    r = repo.contract_lifecycle_apply(pool, PROJ, "8b-nos", "t", "orch-manager", "x",
                                      [{"contract_id": c["id"], "action": "reinstate"}])
    assert r["result"] == "applied"  # settings defaults None -> gate skipped


def test_apply_blocks_activating_unbacked(pool, settings, monkeypatch):
    c = repo.upsert_contract(pool, "GET", "/8b/unbacked", status="deprecated")
    monkeypatch.setattr(contract_drift, "drift_for_contracts",
                        lambda p, s, ids: {"blocking": [], "advisory": [_unbacked(c["id"], "GET", "/8b/unbacked")], "ok": True})
    r = repo.contract_lifecycle_apply(pool, PROJ, "8b-block", "t", "orch-manager", "x",
                                      [{"contract_id": c["id"], "action": "reinstate"}],
                                      settings=settings)
    assert r["result"] == "conflict"
    assert any("route check" in x for x in r["conflicts"])
    assert repo.get_contract(pool, "GET", "/8b/unbacked")["status"] == "deprecated"


def test_accept_route_drift_downgrades_to_warning(pool, settings, monkeypatch):
    c = repo.upsert_contract(pool, "GET", "/8b/accept", status="deprecated")
    monkeypatch.setattr(contract_drift, "drift_for_contracts",
                        lambda p, s, ids: {"blocking": [], "advisory": [_unbacked(c["id"], "GET", "/8b/accept")], "ok": True})
    r = repo.contract_lifecycle_apply(pool, PROJ, "8b-accept", "t", "orch-manager", "x",
                                      [{"contract_id": c["id"], "action": "reinstate"}],
                                      settings=settings, accept_route_drift=True)
    assert r["result"] == "applied"
    assert any("route check" in w for w in r["warnings"])
    assert repo.get_contract(pool, "GET", "/8b/accept")["status"] == "agreed"


def test_supersede_is_not_route_gated(pool, settings, monkeypatch):
    dead = repo.upsert_contract(pool, "GET", "/8b/dead", status="agreed")
    repl = repo.upsert_contract(pool, "GET", "/8b/repl", status="agreed")
    calls = {"n": 0}
    def spy(p, s, ids):
        calls["n"] += 1
        return {"blocking": [_unbacked(dead["id"])], "advisory": [], "ok": True}
    monkeypatch.setattr(contract_drift, "drift_for_contracts", spy)
    r = repo.contract_lifecycle_apply(pool, PROJ, "8b-sup", "t", "orch-manager", "x",
                                      [{"contract_id": dead["id"], "action": "supersede",
                                        "replacement_contract_id": repl["id"]}],
                                      settings=settings, confirm_project=PROJ)
    assert r["result"] == "applied"
    assert calls["n"] == 0  # no activating contracts -> gate short-circuits, drift not queried


def test_preview_reports_route_conflict(pool, settings, monkeypatch):
    c = repo.upsert_contract(pool, "GET", "/8b/prev", status="deprecated")
    monkeypatch.setattr(contract_drift, "drift_for_contracts",
                        lambda p, s, ids: {"blocking": [], "advisory": [_unbacked(c["id"], "GET", "/8b/prev")], "ok": True})
    r = repo.contract_lifecycle_preview(pool, PROJ, "8b-prev", "t", "orch-manager", "x",
                                        [{"contract_id": c["id"], "action": "reinstate"}],
                                        settings=settings)
    assert r["valid"] is False
    assert any("route check" in x for x in r["conflicts"])
