"""DB tests for repository.amend_contract_metadata (metadata correction path)."""
import copy
import pytest
from orchestrator import repository as repo

PROJ = "cadencelms-working"


def _seed(pool, **kw):
    d = dict(method="GET", path="/x", request_ref="", response_dto="D",
             auth="none", owner_team="backend", status="agreed", type_ref="")
    d.update(kw)
    return repo.upsert_contract(pool, **d)


def test_amend_updates_fields_and_hash(pool):
    c = _seed(pool, path="/amend/a", auth="bearer", response_dto="Old")
    old_hash = c["content_hash"]
    r = repo.amend_contract_metadata(
        pool, PROJ, "amend-op-1", "tester", "orch-manager", "fix auth+dto",
        [{"contract_id": c["id"], "auth": "jwt", "response_dto": "NewDTO",
          "type_ref": "packages/contracts/types/x.ts"}])
    assert r["result"] == "amended"
    got = repo.get_contract(pool, "GET", "/amend/a")
    assert got["auth"] == "jwt"
    assert got["response_dto"] == "NewDTO"
    assert got["type_ref"] == "packages/contracts/types/x.ts"
    assert got["status"] == "agreed"           # unchanged
    assert got["content_hash"] != old_hash     # recomputed (response_dto changed)
    hist = repo.contract_lifecycle_history(pool, operation_id="amend-op-1")
    assert len(hist) == 1 and hist[0]["action"] == "amend"
    assert hist[0]["from_status"] == hist[0]["to_status"] == "agreed"


def test_amend_path_rewrite_in_place(pool):
    c = _seed(pool, method="PATCH", path="/wrong/:x/read", status="agreed")
    r = repo.amend_contract_metadata(
        pool, PROJ, "amend-op-2", "tester", "orch-manager", "fix path",
        [{"contract_id": c["id"], "path": "/wrong/:id"}])
    assert r["result"] == "amended"
    assert repo.get_contract(pool, "PATCH", "/wrong/:id")["id"] == c["id"]
    assert repo.get_contract(pool, "PATCH", "/wrong/:x/read") is None


def test_amend_path_collision_rejected(pool):
    a = _seed(pool, method="POST", path="/dup/a")
    b = _seed(pool, method="POST", path="/dup/b")
    r = repo.amend_contract_metadata(
        pool, PROJ, "amend-op-3", "tester", "orch-manager", "collide",
        [{"contract_id": b["id"], "path": "/dup/a"}])
    assert r["result"] == "conflict"
    assert any("already used by" in c for c in r["conflicts"])
    # nothing changed
    assert repo.get_contract(pool, "POST", "/dup/b")["id"] == b["id"]


def test_amend_idempotent_replay(pool):
    c = _seed(pool, path="/amend/idem")
    args = [{"contract_id": c["id"], "auth": "jwt"}]
    r1 = repo.amend_contract_metadata(pool, PROJ, "amend-op-4", "t", "orch-manager", "x", args)
    r2 = repo.amend_contract_metadata(pool, PROJ, "amend-op-4", "t", "orch-manager", "x", args)
    assert r1["audit_op_id"] == r2["audit_op_id"]
    assert len(repo.contract_lifecycle_history(pool, operation_id="amend-op-4")) == 1


def test_amend_project_mismatch_rejected(pool):
    c = _seed(pool, path="/amend/proj")
    r = repo.amend_contract_metadata(pool, "wrong-proj", "amend-op-5", "t", "orch-manager", "x",
                                     [{"contract_id": c["id"], "auth": "jwt"}])
    assert r["result"] == "rejected" and r["reason"] == "project_mismatch"


def test_amend_non_amendable_field_rejected(pool):
    c = _seed(pool, path="/amend/bad")
    r = repo.amend_contract_metadata(pool, PROJ, "amend-op-6", "t", "orch-manager", "x",
                                     [{"contract_id": c["id"], "status": "retired"}])
    assert r["result"] == "conflict"
    assert any("non-amendable" in x for x in r["conflicts"])
