"""WIRE-REFRESH-PROMOTE: `promote()` gating the deploy/CI-CD hook (plan §3.1,
§5 — SECURITY-RELEVANT).

Exercises the REAL `orchestrator.apply.worktree.promote` against a real
Postgres pool (the `pool` fixture) and a real tmp git repo, with the real
`workflow.loader`/`runner`/`escalation` machinery (only the deploy command
itself is fake — `touch <marker>`, never npm/network/API keys).

Mirrors the committed-profile + settings harness from
`tests/test_blocked_on_approval.py`: `_write_and_commit_profile`/`_settings`/
real `pool`.

Per FANOUT-CLAUDE.md this file is written by the WP agent but NEVER executed
by it — self-check with `pytest --collect-only` only; the monitor runs it for
real, serially, at a wave/gate boundary.

Scenarios:
  (a) a promote-step deploy action with no workspace grant (source="repo",
      the default posture) ESCALATES: the merge still happens and is
      recorded (`promoted` event), but the deploy is gated — a
      `pending_actions` row exists, the deploy command never ran, and the
      returned record shows `promote: blocked_on_approval` + a note.
  (b) the SAME shape of action, but declared directly in the workspace
      manifest (source="workspace", self-authorizing per plan §3.2/§5) ->
      the deploy runs for real (observed via the marker file), and the
      record shows `promote_actions: ran`.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import repository as repo
from orchestrator.apply.worktree import promote
from orchestrator.config import load_settings

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *_GIT_ID, *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture()
def promote_repo(tmp_path: Path) -> Path:
    """A scratch git repo standing in for `apply_repo_path`: `main` is the
    currently-checked-out branch `promote()` merges into."""
    path = tmp_path / "target"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("target\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _write_and_commit_profile(promote_repo: Path, yaml_text: str) -> None:
    """Commit `.orchestrator/workflow.yaml` directly on the currently checked
    out branch (main) so it's present at `apply_repo_path` both before AND
    after `promote()`'s merge (the merge doesn't touch it either way — this
    just needs to exist on disk when `promote()` calls `load_effective`)."""
    d = promote_repo / ".orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(yaml_text)
    _git(promote_repo, "add", ".orchestrator/workflow.yaml")
    _git(promote_repo, "commit", "-qm", "test: add workflow profile")


def _make_issue_branch(promote_repo: Path, issue_id: int) -> None:
    """Create `issue-<id>` off `main` with one committed change, then return
    to `main` (the branch `promote()` actually merges into)."""
    branch = f"issue-{issue_id}"
    _git(promote_repo, "checkout", "-b", branch, "main")
    (promote_repo / f"feature-{issue_id}.txt").write_text("feature\n")
    _git(promote_repo, "add", "-A")
    _git(promote_repo, "commit", "-qm", f"feature work for {branch}")
    _git(promote_repo, "checkout", "main")


def _verified_issue(pool, team: str = "backend"):
    goal = repo.create_goal(pool, "wire-refresh-promote", pipeline="pull-1")
    issue = repo.create_issue(
        pool, goal.id, "promote deploy gating",
        "exercises the promote-step deploy gate end to end",
        team=team, pipeline="pull-1",
    )
    repo.append_log(pool, issue.id, "verification", {"passed": True, "branch": f"issue-{issue.id}"})
    return issue


def _settings(promote_repo: Path, workspace_manifest: str = "") -> Any:
    s = copy.deepcopy(load_settings())
    s.apply_repo_path = str(promote_repo)
    s.workflow_profile = "enabled"
    s.workspace_manifest = workspace_manifest
    return s


def _promoted_events(pool, issue_id: int) -> list[Any]:
    return [e for e in repo.recent_events(pool, issue_id, limit=50) if e.event_type == "promoted"]


def _pending_for(pool, issue_id: int, status: str = "pending") -> list[dict[str, Any]]:
    return [r for r in repo.list_pending_actions(pool, status=status) if r["issue_id"] == issue_id]


# ---------------------------------------------------------------------------
# (a) un-granted repo-sourced deploy action escalates; merge still recorded.


def test_promote_deploy_action_escalates_merge_still_recorded(promote_repo, pool):
    issue = _verified_issue(pool)
    _write_and_commit_profile(
        promote_repo,
        'promote:\n  - run: "touch deploy-marker.txt"\n    on_fail: block\n',
    )
    _make_issue_branch(promote_repo, issue.id)

    s = _settings(promote_repo)  # no workspace_manifest -> empty Permissions -> escalate

    record = promote(pool, issue, s, actor="human", note="reviewed")

    # The merge happened and was recorded — a gated deploy never rolls it back.
    assert record["merge_commit"]
    assert record["branch"] == f"issue-{issue.id}"
    promoted = _promoted_events(pool, issue.id)
    assert len(promoted) == 1
    assert promoted[0].payload["merge_commit"] == record["merge_commit"]
    main_files = _git(promote_repo, "ls-tree", "-r", "--name-only", "main").stdout
    assert f"feature-{issue.id}.txt" in main_files

    # The deploy itself is gated: blocked_on_approval, not executed.
    assert record["promote"] == "blocked_on_approval"
    # The deploy-pending message lives in its OWN key — the human's promote
    # note must survive, never be clobbered by the gating annotation.
    assert record["note"] == "reviewed"
    assert "pending approval" in record["promote_note"]
    assert "touch deploy-marker.txt" in record["promote_note"]
    # Resume instruction is included in the promote_note (documented resume path)
    assert "orchestrator apply-promote" in record["promote_note"]
    assert f"--issue {issue.id}" in record["promote_note"]
    assert not (promote_repo / "deploy-marker.txt").exists()

    pending = _pending_for(pool, issue.id)
    assert len(pending) == 1
    assert pending[0]["step"] == "promote"
    assert pending[0]["action"] == "touch deploy-marker.txt"
    assert "promote:human" in pending[0]["requested_by"]

    escalated = [e for e in repo.recent_events(pool, issue.id, limit=50)
                 if e.event_type == "action_escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload["step"] == "promote"


# ---------------------------------------------------------------------------
# (b) a workspace-manifest-declared (source=workspace) deploy action is
# self-authorizing and runs for real.


def test_promote_deploy_action_from_workspace_manifest_runs(promote_repo, pool):
    issue = _verified_issue(pool)
    _make_issue_branch(promote_repo, issue.id)

    manifest = promote_repo.parent / "workspace.yaml"
    manifest.write_text(
        'promote:\n  - run: "touch deploy-marker.txt"\n    on_fail: block\n'
    )
    s = _settings(promote_repo, workspace_manifest=str(manifest))

    record = promote(pool, issue, s, actor="human", note="reviewed")

    assert record["merge_commit"]
    promoted = _promoted_events(pool, issue.id)
    assert len(promoted) == 1

    assert record["promote_actions"] == "ran"
    assert "promote" not in record  # only set on the blocked_on_approval branch
    assert (promote_repo / "deploy-marker.txt").exists()

    assert _pending_for(pool, issue.id) == []
