"""ADR per-issue relevance: forward backlink closure + tag-primary/selector-fallback."""
from orchestrator import adr_rules
from orchestrator.mcp_server.tools_skills import _duplicate_adr


def _adr(key, *, teams=(), work_types=(), related=(), status="accepted"):
    return {"adr_key": key, "domain": key.split("-")[1], "decision": f"decide {key}",
            "title": key, "status": status,
            "applies_to": {"teams": list(teams), "work_types": list(work_types), "repos": []},
            "related": list(related), "supersedes": []}


CATALOG = [
    _adr("ADR-API-001", teams=["backend"], related=["ADR-API-002"]),
    _adr("ADR-API-002", teams=["backend"]),
    _adr("ADR-UI-001", teams=["frontend"], related=["ADR-UI-002"]),
    _adr("ADR-UI-002", teams=["frontend"]),
    _adr("ADR-SEC-001", teams=["backend"], related=["ADR-DATA-001"]),
    _adr("ADR-DATA-001", teams=["backend"]),
    _adr("ADR-DEV-001"),           # universal (no team/work_type)
    _adr("ADR-DEV-004"),           # universal
    _adr("ADR-DRAFT-001", teams=["backend"], status="proposed"),  # inert
]


def test_closure_is_forward_only_no_reverse_leak():
    # Seeding UI-001 pulls its forward-related UI-002, but NOT things that point AT it.
    out = {r["adr_key"] for r in adr_rules.closure({"ADR-UI-001"}, CATALOG)}
    assert out == {"ADR-UI-001", "ADR-UI-002"}
    # SEC-001 -> DATA-001 forward; DATA-001 alone does not pull SEC-001 (no reverse).
    assert {r["adr_key"] for r in adr_rules.closure({"ADR-DATA-001"}, CATALOG)} == {"ADR-DATA-001"}


def test_closure_has_no_depth_cap():
    chain = [_adr("ADR-X-001", related=["ADR-X-002"]), _adr("ADR-X-002", related=["ADR-X-003"]),
             _adr("ADR-X-003", related=["ADR-X-004"]), _adr("ADR-X-004")]
    out = {r["adr_key"] for r in adr_rules.closure({"ADR-X-001"}, chain)}
    assert out == {"ADR-X-001", "ADR-X-002", "ADR-X-003", "ADR-X-004"}  # full chain, uncapped


def test_relevant_selector_fallback_when_untagged():
    out = {r["adr_key"] for r in adr_rules.relevant(CATALOG, work_type="general", team="frontend")}
    # frontend selector floor + universal + forward closure; no backend/proposed rules.
    assert out == {"ADR-UI-001", "ADR-UI-002", "ADR-DEV-001", "ADR-DEV-004"}
    assert "ADR-API-001" not in out and "ADR-DRAFT-001" not in out


def test_relevant_tag_primary_is_precise_plus_universal():
    # Reasoner tagged only API-001; surface = tag + its closure + universal floor,
    # and NOT the rest of the backend selector floor (SEC-001/DATA-001 excluded).
    out = {r["adr_key"] for r in adr_rules.relevant(
        CATALOG, work_type="new-endpoint", team="backend", extra_keys=["ADR-API-001"])}
    assert out == {"ADR-API-001", "ADR-API-002", "ADR-DEV-001", "ADR-DEV-004"}
    assert "ADR-SEC-001" not in out


def test_relevant_never_seeds_proposed_rules():
    out = {r["adr_key"] for r in adr_rules.relevant(
        CATALOG, work_type="general", team="backend", extra_keys=["ADR-DRAFT-001"])}
    assert "ADR-DRAFT-001" not in out  # inert proposals are not valid tags


def test_duplicate_adr_catches_near_copy_and_passes_novel():
    existing = [_adr("ADR-API-001")]
    existing[0]["title"] = "API design standards for paths and methods"
    existing[0]["decision"] = "Follow REST path and method naming standards"
    assert _duplicate_adr(existing, "API",
                          "API design standards paths methods",
                          "Follow REST path and method naming standards") == "ADR-API-001"
    assert _duplicate_adr(existing, "CAL",
                          "Calendar sync minimization policy",
                          "Minimize external calendar titles and two-way sync type") is None
