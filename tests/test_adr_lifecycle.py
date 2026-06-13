"""ADR numbering (collision-free after delete) + update_adr (SoT edit)."""

import pytest

from orchestrator import repository as repo


def test_create_adr_numbering_survives_delete(pool):
    a = repo.create_adr(pool, "ORCH", "one", "d1", status="proposed")
    b = repo.create_adr(pool, "ORCH", "two", "d2", status="proposed")
    assert a["adr_key"] == "ADR-ORCH-001" and b["adr_key"] == "ADR-ORCH-002"
    repo.delete_adr(pool, "ADR-ORCH-001")            # count drops to 1
    c = repo.create_adr(pool, "ORCH", "three", "d3", status="proposed")
    # max-suffix + 1, NOT count+1 (which would collide with the live ADR-ORCH-002)
    assert c["adr_key"] == "ADR-ORCH-003"


def test_update_adr_changes_content_preserves_status(pool):
    a = repo.create_adr(pool, "ORCH", "loop rule", "old decision", status="accepted")
    u = repo.update_adr(pool, a["adr_key"], decision="new decision", context="why")
    assert u["decision"] == "new decision"
    assert u["context"] == "why"
    assert u["status"] == "accepted"                 # live rule stays live


def test_update_adr_partial_leaves_other_fields(pool):
    a = repo.create_adr(pool, "UI", "t", "keep me", status="proposed")
    u = repo.update_adr(pool, a["adr_key"],
                        applies_to={"work_types": [], "teams": ["frontend"],
                                    "repos": ["web-repo"]})
    assert u["applies_to"]["teams"] == ["frontend"]
    assert u["decision"] == "keep me"                # untouched


def test_update_adr_missing_raises(pool):
    with pytest.raises(ValueError):
        repo.update_adr(pool, "ADR-NOPE-001", decision="x")
