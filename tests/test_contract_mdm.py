"""Contract master-data management: staging diff, accept/reject, consumers,
and the /contracts dashboard."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator import repository as repo
from orchestrator.dashboard.app import create_app


# --- staging diff ----------------------------------------------------------- #

def test_stage_from_seed_classifies_add_modify_remove_skip(pool):
    # an accepted (live) contract that the seed keeps unchanged, one it changes,
    # and one it drops; plus a brand-new endpoint.
    repo.upsert_contract(pool, "GET", "/keep", request_ref="kSchema", status="live")
    repo.upsert_contract(pool, "GET", "/change", request_ref="old", status="live")
    repo.upsert_contract(pool, "GET", "/gone", request_ref="g", status="live")
    seed = [
        {"method": "GET", "path": "/keep", "request_ref": "kSchema"},   # same -> skip
        {"method": "GET", "path": "/change", "request_ref": "new"},     # diff -> modify
        {"method": "POST", "path": "/brand-new", "request_ref": "nSchema"},  # add
    ]
    counts = repo.stage_from_seed(pool, seed, full=True)
    assert counts == {"add": 1, "modify": 1, "remove": 1, "skip": 1}
    props = {(p["method"], p["path"]): p["change_type"] for p in repo.list_proposals(pool)}
    assert props == {("GET", "/change"): "modify", ("POST", "/brand-new"): "add",
                     ("GET", "/gone"): "remove"}


def test_stage_is_idempotent(pool):
    repo.upsert_contract(pool, "GET", "/a", request_ref="x", status="live")
    seed = [{"method": "GET", "path": "/a", "request_ref": "y"}]
    repo.stage_from_seed(pool, seed)
    repo.stage_from_seed(pool, seed)  # re-stage shouldn't duplicate the pending proposal
    assert len(repo.list_proposals(pool)) == 1


# --- accept / reject -------------------------------------------------------- #

def test_accept_proposal_writes_contract_and_satisfies_gate(pool):
    repo.stage_from_seed(pool, [{"method": "POST", "path": "/widgets",
                                 "request_ref": "createWidget", "status": "live"}])
    assert repo.contract_satisfied(pool, "POST", "/widgets") is False  # staged, not accepted
    c = repo.accept_proposal(pool, "POST", "/widgets")
    assert c["status"] == "live" and c["request_ref"] == "createWidget"
    assert repo.contract_satisfied(pool, "POST", "/widgets") is True
    assert repo.get_proposal(pool, "POST", "/widgets") is None  # no longer pending


def test_accept_removal_deprecates(pool):
    repo.upsert_contract(pool, "GET", "/old", status="live")
    repo.stage_from_seed(pool, [])             # full seed without /old -> remove proposal
    assert repo.get_proposal(pool, "GET", "/old")["change_type"] == "remove"
    repo.accept_proposal(pool, "GET", "/old")
    assert repo.get_contract(pool, "GET", "/old")["status"] == "deprecated"


def test_consumers_of_from_issue_deps(pool):
    goal = repo.create_goal(pool, "g")
    issue = repo.create_issue(pool, goal.id, "fe work", team="frontend")
    repo.add_issue_contract_deps(pool, issue.id, [{"method": "GET", "path": "/x"}])
    assert repo.consumers_of(pool, "GET", "/x") == ["frontend"]


# --- dashboard -------------------------------------------------------------- #

def _client(pool, settings):
    return TestClient(create_app(pool, settings))


def test_contracts_page_renders_states_and_diff(pool, settings):
    repo.upsert_contract(pool, "GET", "/live", request_ref="r", status="live")  # up-to-date
    repo.stage_from_seed(pool, [{"method": "GET", "path": "/live", "request_ref": "r"},
                                {"method": "POST", "path": "/new", "request_ref": "n"}])
    html = _client(pool, settings).get("/contracts").text
    assert "Contracts" in html and "Current" in html and "Proposed" in html
    assert "POST /new" in html and "awaiting acceptance" in html
    assert "Create goals &amp; issues from changes" in html


def test_contracts_accept_route(pool, settings):
    repo.stage_from_seed(pool, [{"method": "POST", "path": "/widgets",
                                 "request_ref": "w", "status": "live"}])
    r = _client(pool, settings).post("/contracts/accept",
                                     data={"method": "POST", "path": "/widgets"},
                                     follow_redirects=False)
    assert r.status_code == 303
    assert repo.contract_satisfied(pool, "POST", "/widgets") is True


def test_contracts_accept_direct_proposed_row(pool, settings):
    repo.propose_contract(pool, "GET", "/clinicians",
                          response_dto="ClinicianListResponse",
                          owner_team="backend", auth="role")
    html = _client(pool, settings).get("/contracts").text
    assert "GET /clinicians" in html
    assert "Accept as agreed" in html

    r = _client(pool, settings).post("/contracts/accept",
                                     data={"method": "GET", "path": "/clinicians"},
                                     follow_redirects=False)
    assert r.status_code == 303
    c = repo.get_contract(pool, "GET", "/clinicians")
    assert c["status"] == "agreed"
    assert repo.contract_satisfied(pool, "GET", "/clinicians") is True


def test_accept_with_issue_routes_to_owner_and_consumer(pool, settings):
    # a frontend issue consumes the endpoint -> consumer = frontend; owner = backend
    goal = repo.create_goal(pool, "g")
    fe = repo.create_issue(pool, goal.id, "fe consumes", team="frontend")
    repo.add_issue_contract_deps(pool, fe.id, [{"method": "GET", "path": "/shared"}])
    repo.stage_from_seed(pool, [{"method": "GET", "path": "/shared", "request_ref": "s",
                                 "owner_team": "backend", "status": "live"}])
    before = {g.id for g in repo.list_all_goals(pool)}
    r = _client(pool, settings).post("/contracts/accept_with_issue",
                                     data={"method": "GET", "path": "/shared"},
                                     follow_redirects=False)
    assert r.status_code == 303
    new_goals = [g for g in repo.list_all_goals(pool) if g.id not in before]
    teams = set()
    for g in new_goals:
        for i in repo.list_issues(pool, goal_id=g.id):
            teams.add(i.team)
    assert {"backend", "frontend"} <= teams        # owner + consumer both got work
    assert repo.get_contract(pool, "GET", "/shared")["status"] == "agreed"
