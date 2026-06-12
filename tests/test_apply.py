"""Apply/verify leg (slice F): worktree apply, verification events, human-gated
promotion, and — most importantly — that the flag-off default changes nothing."""

import copy
import subprocess

import pytest

from orchestrator import repository as repo
from orchestrator.agents.reasoning import StubReasoner
from orchestrator.apply.worktree import apply_and_verify, promote
from orchestrator.engine.loop import Engine

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *_GIT_ID, *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture()
def target_repo(tmp_path):
    """A scratch git repo standing in for the codebase artifacts apply to."""
    path = tmp_path / "target"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("target\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _run_to_done(settings, pool):
    repo.register_agent(pool, "backend", "dev")
    repo.register_agent(pool, "backend", "qa")
    repo.create_goal(pool, "Apply demo")
    Engine(settings, pool, reasoner=StubReasoner()).run(max_ticks=40)
    return repo.list_issues(pool)


def _events(pool, issue_id, kind):
    return [e for e in repo.issue_timeline(pool, issue_id) if e.event_type == kind]


def test_flag_off_by_default_no_verification(settings, pool):
    assert settings.apply_enabled is False
    issues = _run_to_done(settings, pool)
    assert issues and all(i.state == "done" for i in issues)
    for i in issues:
        assert _events(pool, i.id, "verification") == []
        assert _events(pool, i.id, "promoted") == []


def test_apply_and_verify_pass(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "test -f generated/issue-*.txt || exit 1; true"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")]
    assert impl
    for i in impl:
        vs = _events(pool, i.id, "verification")
        assert vs, "qa_gate work step must log a verification event"
        v = vs[-1].payload
        assert v["passed"] is True and v["branch"] == f"issue-{i.id}"
        # the artifact is committed on the branch, not on main
        files = _git(target_repo, "ls-tree", "-r", "--name-only",
                     f"issue-{i.id}").stdout
        assert f"generated/issue-{i.id}.txt" in files
        main_files = _git(target_repo, "ls-tree", "-r", "--name-only", "main").stdout
        assert f"generated/issue-{i.id}.txt" not in main_files


def test_verify_failure_recorded(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "false"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    v = _events(pool, impl.id, "verification")[-1].payload
    assert v["passed"] is False and v["returncode"] == 1


def test_promote_requires_passing_verification(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "false"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    with pytest.raises(ValueError, match="did not pass"):
        promote(pool, impl, s)
    assert _events(pool, impl.id, "promoted") == []


def test_promote_merges_verified_branch(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "true"
    issues = _run_to_done(s, pool)
    impl = [i for i in issues if i.title.startswith("Implement")][0]
    record = promote(pool, impl, s, note="reviewed")
    assert record["branch"] == f"issue-{impl.id}"
    main_files = _git(target_repo, "ls-tree", "-r", "--name-only", "main").stdout
    assert f"generated/issue-{impl.id}.txt" in main_files
    assert _events(pool, impl.id, "promoted")[-1].payload["note"] == "reviewed"
    # nothing was pushed anywhere: the scratch repo has no remotes
    remotes = _git(target_repo, "remote").stdout.strip()
    assert remotes == ""


def test_promote_without_verification_raises(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_repo_path = str(target_repo)
    issues = _run_to_done(s, pool)  # flag off → no verification events
    with pytest.raises(ValueError, match="no verification"):
        promote(pool, issues[0], s)


def test_missing_artifact_skips(settings, pool, target_repo):
    s = copy.deepcopy(settings)
    s.apply_enabled = True
    s.apply_repo_path = str(target_repo)
    s.verify_cmd = "true"
    goal = repo.create_goal(pool, "No artifact")
    repo.set_goal_state(pool, goal.id, "active")
    issue = repo.create_issue(pool, goal.id, "Bare issue", pipeline="hotfix")
    # qa_gate work step on an issue whose implementation never produced code:
    issue = repo.update_state(pool, issue.id, "in_progress", gate_type="qa_gate")
    result = apply_and_verify(pool, issue, s)
    assert result["passed"] is False and "no code_generated" in result["skipped"]
