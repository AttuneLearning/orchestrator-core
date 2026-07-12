"""qwen3-coder guardrails: G1/G7 real-commit + diff lints, G4 readiness, G8 escalate."""
import subprocess

import pytest

from orchestrator import repository as repo
from orchestrator.config import load_settings
from orchestrator.mcp_server.tools_issues import _verify_commit_real, _issue_ready


def _git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True)


@pytest.fixture
def gitrepo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "base.txt").write_text("base\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


def _settings(repo_path):
    s = load_settings()
    s.promote_repo_path = str(repo_path)
    s.apply_repo_path = ""
    s.promote_branch = "main"
    return s


def _commit_on_issue(d, path, content):
    _git(d, "checkout", "-q", "-B", "issue-1", "main")
    f = d / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "work")


# --- G1: real commit -------------------------------------------------------- #

def test_g1_passes_a_real_nonempty_commit(gitrepo):
    _commit_on_issue(gitrepo, "src/x.ts", "export const x = 1;\n")
    _verify_commit_real(_settings(gitrepo), 1, "")  # must not raise


def test_g1_rejects_missing_branch(gitrepo):
    with pytest.raises(ValueError, match="does not exist"):
        _verify_commit_real(_settings(gitrepo), 1, "")


def test_g1_rejects_empty_diff(gitrepo):
    _git(gitrepo, "checkout", "-q", "-B", "issue-1", "main")  # branch == main, no changes
    with pytest.raises(ValueError, match="no diff"):
        _verify_commit_real(_settings(gitrepo), 1, "")


def test_g1_skips_when_no_repo_configured():
    s = load_settings()
    s.promote_repo_path = ""
    s.apply_repo_path = ""
    _verify_commit_real(s, 1, "")  # must not raise (hermetic)


# --- G7: output-quality lints on the diff ----------------------------------- #

def test_g7_rejects_placeholder_test(gitrepo):
    _commit_on_issue(gitrepo, "src/x.test.ts", "it('x', () => { expect(true).toBe(true); });\n")
    with pytest.raises(ValueError, match="placeholder test"):
        _verify_commit_real(_settings(gitrepo), 1, "")


def test_g7_rejects_raw_fetch_in_web_component(gitrepo):
    _commit_on_issue(gitrepo, "apps/web/src/pages/Foo.tsx",
                     "export async function load() { return fetch('/shares'); }\n")
    with pytest.raises(ValueError, match="raw fetch"):
        _verify_commit_real(_settings(gitrepo), 1, "")


def test_g7_allows_fetch_in_shared_api_client(gitrepo):
    _commit_on_issue(gitrepo, "apps/web/src/shared/api/fooApi.ts",
                     "export const f = () => fetch('/x');\n")
    _verify_commit_real(_settings(gitrepo), 1, "")  # shared/api is the allowed place


# --- GAP-1: lane enforcement on the diff ------------------------------------ #

def test_lane_backend_rejects_frontend_files(gitrepo):
    _commit_on_issue(gitrepo, "apps/web/src/pages/Foo.tsx", "export const x = 1;\n")
    with pytest.raises(ValueError, match="outside the backend lane"):
        _verify_commit_real(_settings(gitrepo), 1, "", team="backend")


def test_lane_backend_allows_api_and_contracts(gitrepo):
    _commit_on_issue(gitrepo, "apps/api/src/x.ts", "export const x = 1;\n")
    (gitrepo / "packages/contracts/src").mkdir(parents=True)
    (gitrepo / "packages/contracts/src/y.ts").write_text("export const y = 2;\n")
    (gitrepo / "contracts.seed.json").write_text("[]\n")
    _git(gitrepo, "add", "-A")
    _git(gitrepo, "commit", "-qm", "more")
    _verify_commit_real(_settings(gitrepo), 1, "", team="backend")  # must not raise


def test_lane_frontend_rejects_contracts_edit(gitrepo):
    _commit_on_issue(gitrepo, "packages/contracts/src/shares.ts", "export const s = 1;\n")
    with pytest.raises(ValueError, match="outside the frontend lane"):
        _verify_commit_real(_settings(gitrepo), 1, "", team="frontend")


def test_lane_frontend_rejects_root_toolchain(gitrepo):
    _commit_on_issue(gitrepo, "package.json", '{"name": "x"}\n')
    with pytest.raises(ValueError, match="outside the frontend lane"):
        _verify_commit_real(_settings(gitrepo), 1, "", team="frontend")


def test_lane_senior_and_unknown_teams_unrestricted(gitrepo):
    _commit_on_issue(gitrepo, "package.json", '{"name": "x"}\n')
    _verify_commit_real(_settings(gitrepo), 1, "", team="senior")  # must not raise
    _verify_commit_real(_settings(gitrepo), 1, "", team="")        # legacy callers


# --- G4: issue readiness ---------------------------------------------------- #

def test_g4_ready_when_actionable():
    ok, _ = _issue_ready("Add POST /clients",
                         "Implement POST /clients in apps/api with an integration test.")
    assert ok


def test_g4_not_ready_when_too_thin():
    ok, why = _issue_ready("x", "make it work")
    assert not ok and "thin" in why


def test_g4_not_ready_without_actionable_signal():
    ok, why = _issue_ready("Improve things",
                           "please make the whole thing feel better and nicer overall")
    assert not ok and "actionable signal" in why


# --- G8/G6: escalate to senior --------------------------------------------- #

def test_g8_escalate_assigns_to_senior_dev(pool):
    senior = repo.register_agent(pool, "senior", "dev", "external")
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    issue = repo.create_issue(pool, goal.id, "i", pipeline="pull-1", team="backend")
    aid = repo.escalate_to_senior(pool, issue.id)
    assert aid == senior.id
    assert repo.get_issue(pool, issue.id).assigned_agent == senior.id
    assert repo.get_issue(pool, issue.id).team == "backend"  # keeps its lane


def test_g8_escalate_unassigns_when_no_senior(pool):
    goal = repo.create_goal(pool, "g", pipeline="pull-1")
    issue = repo.create_issue(pool, goal.id, "i", pipeline="pull-1", team="backend")
    agent = repo.register_agent(pool, "backend", "dev", "external")
    repo.claim_issue(pool, issue.id, agent.id)
    assert repo.escalate_to_senior(pool, issue.id) is None
    assert repo.get_issue(pool, issue.id).assigned_agent is None
