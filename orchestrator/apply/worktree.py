"""Worktree apply + verify + human-gated promotion.

apply_and_verify() is called by the engine's qa_gate work step when
settings.apply_enabled. promote() is called only by the CLI (human directive).
All git commands run with signing disabled and an explicit identity so they
work in any environment; nothing here ever pushes.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Optional

from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings
from ..models import Issue

WORKTREES_DIR = Path("/tmp/orchestrator-worktrees")
VERIFY_TIMEOUT_S = 300
_GIT_ID = ["-c", "user.email=orchestrator@local", "-c", "user.name=orchestrator",
           "-c", "commit.gpgsign=false"]


def _git(repo_path: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *_GIT_ID, *args],
        capture_output=True, text=True, timeout=60, check=check,
    )


def _branch(issue: Issue) -> str:
    return f"issue-{issue.id}"


def _worktree_path(issue: Issue, base: str | Path) -> Path:
    # Scoped per base repo: two orchestrators (or two test repos) applying the
    # same issue id must not share a worktree.
    base_key = hashlib.md5(str(Path(base).resolve()).encode()).hexdigest()[:8]
    return WORKTREES_DIR / base_key / _branch(issue)


def _latest_artifact(pool: ConnectionPool, issue_id: int) -> Optional[str]:
    for e in repo.recent_events(pool, issue_id, limit=200):
        if e.event_type == "code_generated" and e.payload.get("content"):
            return e.payload["content"]
    return None


def apply_and_verify(pool: ConnectionPool, issue: Issue, settings: Settings) -> dict[str, Any]:
    """Apply the issue's latest artifact in an isolated worktree and verify it.

    Always returns (and logs) a verification dict; missing config or artifact is
    reported as a skipped verification rather than an error, so the gate
    reviewer sees exactly what happened.
    """
    result: dict[str, Any]
    if not settings.apply_repo_path:
        result = {"passed": False, "skipped": "apply_repo_path not configured"}
    else:
        artifact = _latest_artifact(pool, issue.id)
        if artifact is None:
            result = {"passed": False, "skipped": "no code_generated artifact"}
        else:
            result = _apply_in_worktree(issue, artifact, settings)
    repo.append_log(pool, issue.id, "verification", result)
    return result


def _apply_in_worktree(issue: Issue, artifact: str, settings: Settings) -> dict[str, Any]:
    base = settings.apply_repo_path
    branch = _branch(issue)
    wt = _worktree_path(issue, base)

    wt.parent.mkdir(parents=True, exist_ok=True)
    if not wt.exists():
        # -B: reuse/reset the branch if a previous attempt left it behind
        _git(base, "worktree", "add", "-B", branch, str(wt))

    target = wt / "generated" / f"issue-{issue.id}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(artifact)
    _git(wt, "add", "-A")
    commit = _git(wt, "commit", "-m", f"apply artifact for issue #{issue.id}",
                  check=False)  # empty diff on re-run is fine
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    if not settings.verify_cmd:
        return {"passed": False, "skipped": "verify_cmd not configured",
                "branch": branch, "commit": sha}

    try:
        proc = subprocess.run(
            settings.verify_cmd, shell=True, cwd=wt,
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT_S,
        )
        return {
            "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "branch": branch,
            "commit": sha,
            "committed": commit.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "error": f"verify timed out after {VERIFY_TIMEOUT_S}s",
                "branch": branch, "commit": sha}


def promote(pool: ConnectionPool, issue: Issue, settings: Settings,
            actor: str = "human", note: str = "") -> dict[str, Any]:
    """Merge the issue's worktree branch into the base repo's current branch.

    Human directive only — the engine never calls this. Requires the latest
    verification event to have passed. Local merge; never pushes.
    """
    if not settings.apply_repo_path:
        raise ValueError("apply_repo_path not configured")
    verification = next(
        (e.payload for e in repo.recent_events(pool, issue.id, limit=200)
         if e.event_type == "verification"),
        None,
    )
    if verification is None:
        raise ValueError(f"issue {issue.id} has no verification event")
    if not verification.get("passed"):
        raise ValueError(f"issue {issue.id}'s latest verification did not pass")

    branch = _branch(issue)
    merge = _git(settings.apply_repo_path, "merge", "--no-ff", "-m",
                 f"promote issue #{issue.id} ({note or 'no note'})", branch,
                 check=False)
    if merge.returncode != 0:
        raise RuntimeError(f"merge failed: {merge.stderr[:500]}")
    sha = _git(settings.apply_repo_path, "rev-parse", "HEAD").stdout.strip()
    record = {"actor": actor, "note": note, "branch": branch, "merge_commit": sha}
    repo.append_log(pool, issue.id, "promoted", record)
    return record
