"""Contract store: repository CRUD + the contract_* MCP tools."""

from orchestrator import repository as repo
from orchestrator.mcp_server import tools_contracts


class _Recorder:
    """Captures @mcp.tool()-decorated functions so we can call them directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(pool):
    rec = _Recorder()
    tools_contracts.register(rec, pool)
    return rec.tools


# --- repository ------------------------------------------------------------- #

def test_upsert_is_idempotent_on_method_path(pool):
    a = repo.upsert_contract(pool, "get", "/system/status", response_dto="SystemStatusDTO",
                             status="live")
    b = repo.upsert_contract(pool, "GET", "/system/status", response_dto="SystemStatusDTO2",
                             status="live")
    assert a["id"] == b["id"]                       # same row, not a duplicate
    assert a["method"] == "GET"                     # method normalised to upper
    assert b["response_dto"] == "SystemStatusDTO2"  # fields updated in place
    assert b["content_hash"] != a["content_hash"]   # hash recomputed
    assert len(repo.list_contracts(pool)) == 1


def test_type_ref_threads_through_seed_stage_and_accept(pool):
    """Phase 3: type_ref (pointer into packages/contracts) survives the seed →
    stage → accept path and lands on the live contract."""
    rows = [{"method": "GET", "path": "/courses", "response_dto": "CourseDTO",
             "status": "live", "type_ref": "packages/contracts/types/curriculum.ts"}]
    repo.stage_from_seed(pool, rows, full=True)
    prop = repo.get_proposal(pool, "GET", "/courses")
    assert prop["type_ref"] == "packages/contracts/types/curriculum.ts"  # carried into staging
    contract = repo.accept_proposal(pool, "GET", "/courses")
    assert contract["type_ref"] == "packages/contracts/types/curriculum.ts"  # and onto the contract
    assert repo.get_contract(pool, "GET", "/courses")["type_ref"] == \
        "packages/contracts/types/curriculum.ts"


def test_type_ref_is_metadata_not_part_of_content_hash(pool):
    """Refreshing type_ref must not register as contract drift (it is not hashed)."""
    a = repo.upsert_contract(pool, "GET", "/users", response_dto="UserDTO", status="live",
                             type_ref="packages/contracts/types/auth.ts")
    b = repo.upsert_contract(pool, "GET", "/users", response_dto="UserDTO", status="live",
                             type_ref="packages/contracts/types/organization.ts")
    assert a["content_hash"] == b["content_hash"]      # shape unchanged → no drift
    assert b["type_ref"] == "packages/contracts/types/organization.ts"  # pointer updated in place


def test_contract_satisfied_only_for_agreed_or_live(pool):
    repo.upsert_contract(pool, "POST", "/widgets", status="proposed")
    assert repo.contract_satisfied(pool, "POST", "/widgets") is False
    repo.set_contract_status(pool, "POST", "/widgets", "agreed")
    assert repo.contract_satisfied(pool, "POST", "/widgets") is True
    repo.upsert_contract(pool, "GET", "/widgets", status="live")
    assert repo.contract_satisfied(pool, "GET", "/widgets") is True


def test_propose_never_downgrades_existing(pool):
    repo.upsert_contract(pool, "GET", "/x", status="agreed")
    again = repo.propose_contract(pool, "GET", "/x")
    assert again["status"] == "agreed"  # left untouched, not reset to proposed


def test_set_status_missing_raises(pool):
    import pytest
    with pytest.raises(ValueError):
        repo.set_contract_status(pool, "GET", "/nope", "agreed")


def test_issue_contract_deps_roundtrip(pool):
    goal = repo.create_goal(pool, "G")
    issue = repo.create_issue(pool, goal.id, "consume endpoint")
    repo.add_issue_contract_deps(pool, issue.id,
                                 [{"method": "get", "path": "/a"},
                                  {"method": "POST", "path": "/b"}])
    deps = repo.list_issue_contract_deps(pool, issue.id)
    assert {(d["method"], d["path"]) for d in deps} == {("GET", "/a"), ("POST", "/b")}
    assert all(d["satisfied"] is False for d in deps)
    repo.mark_contract_deps_satisfied(pool, issue.id)
    assert all(d["satisfied"] for d in repo.list_issue_contract_deps(pool, issue.id))


# --- MCP tools -------------------------------------------------------------- #

def test_mcp_propose_agree_get_list(pool):
    tools = _tools(pool)
    created = tools["contract_propose"](method="GET", path="/system/status",
                                        response_dto="SystemStatusDTO")
    assert created["status"] == "proposed" and created["method"] == "GET"
    agreed = tools["contract_agree"](method="GET", path="/system/status")
    assert agreed["status"] == "agreed"
    assert tools["contract_get"](method="get", path="/system/status")["status"] == "agreed"
    assert [c["path"] for c in tools["contract_list"](status="agreed")] == ["/system/status"]


def test_mcp_upsert_registers_live(pool):
    tools = _tools(pool)
    row = tools["contract_upsert"](method="GET", path="/health", status="live",
                                   owner_team="backend")
    assert row["status"] == "live"
    assert tools["contract_list"](owner_team="backend")[0]["path"] == "/health"
