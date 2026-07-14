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


def test_create_adr_case_insensitive_counter(pool):
    """Creating ADRs with different-case domains (DEV vs dev) generates
    sequential numbers without collision — case normalization ensures a
    shared counter."""
    a = repo.create_adr(pool, "DEV", "upper case", "d1", status="proposed")
    b = repo.create_adr(pool, "dev", "lower case", "d2", status="proposed")
    assert a["adr_key"] == "ADR-DEV-001"
    assert b["adr_key"] == "ADR-DEV-002"
    # Verify both are stored with lowercase domain internally
    assert a["domain"] == "dev"
    assert b["domain"] == "dev"


def test_create_adr_mixed_case_row_counted(pool):
    """Pre-existing mixed-case domain rows are counted by the case-insensitive
    WHERE clause, so subsequent normalized creates continue the sequence."""
    # Seed a row with uppercase domain via create_adr
    a = repo.create_adr(pool, "BUILD", "initial", "d1", status="proposed")
    assert a["adr_key"] == "ADR-BUILD-001"
    assert a["domain"] == "build"  # normalized to lowercase
    # Create another with lowercase; counter continues to 002
    b = repo.create_adr(pool, "build", "second", "d2", status="proposed")
    assert b["adr_key"] == "ADR-BUILD-002"
    assert b["domain"] == "build"


def test_list_adrs_domain_filter_case_insensitive(pool):
    """list_adrs domain filter must work case-insensitively; ADRs are created
    with normalized (lowercased) domains, so queries with mixed-case domain must match."""
    # Create an ADR with uppercase domain; it's stored as lowercase internally
    adr = repo.create_adr(pool, "DEV", "security rule", "d1", status="accepted")
    assert adr["domain"] == "dev"

    # list_adrs with lowercase domain filter should find it
    results_lower = repo.list_adrs(pool, domain="dev")
    assert len(results_lower) == 1
    assert results_lower[0]["adr_key"] == "ADR-DEV-001"

    # list_adrs with uppercase domain filter should also find it
    results_upper = repo.list_adrs(pool, domain="DEV")
    assert len(results_upper) == 1
    assert results_upper[0]["adr_key"] == "ADR-DEV-001"

    # Both should return the same row
    assert results_lower[0]["id"] == results_upper[0]["id"]
