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


def _committed_branch(pool: ConnectionPool, issue_id: int) -> Optional[str]:
    """The real branch the pull-worker committed (event payloads carry it; names
    vary, e.g. 'issue-154' vs 'issue-152-grade-override-ui'), newest first."""
    for e in repo.recent_events(pool, issue_id, limit=200):
        if e.event_type == "code_committed" and (e.payload or {}).get("branch"):
            return e.payload["branch"]
    return None


def _ref_exists(repo_path: str | Path, ref: str) -> bool:
    return _git(repo_path, "rev-parse", "--verify", "--quiet", ref,
                check=False).returncode == 0


def _integration_worktree(settings: Settings) -> Path:
    base_key = hashlib.md5(str(Path(settings.promote_repo_path).resolve()).encode()).hexdigest()[:8]
    return WORKTREES_DIR / base_key / f"_integration_{settings.promote_branch}"


def _list_worktrees(repo_path: str | Path) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` into [{path, branch|None}]."""
    out = _git(repo_path, "worktree", "list", "--porcelain", check=False).stdout
    wts: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                wts.append(cur)
            cur = {"path": line[len("worktree "):], "branch": None}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
    if cur:
        wts.append(cur)
    return wts


def _worktree_clean(wt_path: str | Path) -> bool:
    """True if no TRACKED changes and not mid merge/rebase (untracked files ignored)."""
    if (_git(wt_path, "diff", "--quiet", check=False).returncode != 0 or
            _git(wt_path, "diff", "--cached", "--quiet", check=False).returncode != 0):
        return False
    gitdir = _git(wt_path, "rev-parse", "--git-dir", check=False).stdout.strip()
    base = Path(wt_path) / gitdir if not Path(gitdir).is_absolute() else Path(gitdir)
    return not any((base / m).exists() for m in
                   ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def sync_downstream(pool: ConnectionPool, issue: Issue, settings: Settings) -> list[dict[str, Any]]:
    """Bring promote_branch into each downstream worktree after a promote, so the next
    team sees integrated work. SAFE: skips the integrator, the main checkout, detached
    heads, dirty trees, and active `issue-*` work branches; fast-forwards or merges (never
    rebases/rewrites); aborts on conflict leaving the tree pristine. Never pushes."""
    base, target = settings.promote_repo_path, settings.promote_branch
    integ = str(_integration_worktree(settings).resolve())
    results: list[dict[str, Any]] = []
    for w in _list_worktrees(base):
        path, br = w.get("path"), w.get("branch")
        if not path or str(Path(path).resolve()) == integ:
            continue
        if br is None:
            results.append({"worktree": path, "skipped": "detached HEAD"}); continue
        if br == target:
            continue  # the integration/main checkout itself
        if br.startswith("issue-"):
            results.append({"worktree": path, "branch": br, "skipped": "active work branch"}); continue
        if not _worktree_clean(path):
            results.append({"worktree": path, "branch": br, "skipped": "uncommitted/mid-merge"}); continue
        ff = _git(path, "merge", "--ff-only", target, check=False)
        if ff.returncode == 0:
            results.append({"worktree": path, "branch": br, "synced": "ff"}); continue
        mg = _git(path, "merge", "--no-edit", target, check=False)
        if mg.returncode == 0:
            results.append({"worktree": path, "branch": br, "synced": "merge"})
        else:
            _git(path, "merge", "--abort", check=False)
            results.append({"worktree": path, "branch": br, "skipped": "merge conflict (left clean)"})
    if results:
        repo.append_log(pool, issue.id, "downstream_synced", {"target": target, "results": results})
    return results


def _on_branch(path: str, branch: str) -> bool:
    """True if the git checkout at `path` currently has `branch` checked out."""
    r = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return r.returncode == 0 and r.stdout.strip() == branch


def _heal_lockfile(wt: str | Path, issue: Issue, target: str) -> Optional[dict[str, Any]]:
    """Resolve a `package-lock.json`-only merge conflict (the common cost of parallel
    devs each running `npm install`) by regenerating the lockfile from the already-merged
    `package.json`, then completing the merge commit. Returns the promote record on
    success, or None if it couldn't heal (caller then aborts + bounces to a human).
    Safe: only invoked when package.json merged cleanly and the lockfile is the sole conflict."""
    if _git(wt, "checkout", "--theirs", "package-lock.json", check=False).returncode != 0:
        return None
    _git(wt, "add", "package-lock.json", check=False)
    try:  # regenerate the lockfile to match the merged package.json (no node_modules write)
        inst = subprocess.run(
            ["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"],
            cwd=str(wt), capture_output=True, text=True, timeout=300)
    except Exception:  # noqa: BLE001
        return None
    if inst.returncode != 0:
        return None
    _git(wt, "add", "package-lock.json", check=False)
    if _git(wt, "commit", "--no-edit", check=False).returncode != 0:
        return None
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    return {"promoted": True, "branch": _branch(issue), "target": target,
            "merge_commit": sha, "healed": "package-lock.json regenerated"}


def auto_promote_on_done(pool: ConnectionPool, issue: Issue,
                         settings: Settings) -> dict[str, Any]:
    """Merge a completed issue's committed branch into promote_branch so the next
    team sees the work. LOCAL merge only — never pushes. Returns one of:
      {'promoted': True, 'merge_commit', 'branch', 'target'}
      {'skipped': <why>}                      (nothing to merge / already merged)
      {'promoted': False, 'conflict': True}   (caller should bounce to dev)
    A conflict is aborted cleanly (working tree left pristine) and logged.
    """
    if not settings.promote_repo_path:
        return {"skipped": "promote_repo_path not configured"}
    base = settings.promote_repo_path
    target = settings.promote_branch
    branch = _committed_branch(pool, issue.id) or _branch(issue)
    if branch == target:
        return {"skipped": f"branch is the target ({target})"}
    if not _ref_exists(base, branch):
        return {"skipped": f"no committed branch for issue (tried '{branch}')"}

    # Pick where to merge. Git forbids checking out `target` in a second worktree when
    # it's already checked out — exactly the case when promote_repo_path IS the target's
    # working checkout (e.g. .../tendcharting sits on 'main'). Then merge directly there;
    # otherwise spin up a dedicated integration worktree and sync it to the target tip.
    if _on_branch(base, target):
        wt = Path(base)
        # Only TRACKED changes make an in-place merge unsafe; untracked files are fine
        # (git merge aborts on its own if one would be overwritten). --untracked-files=no
        # avoids blocking on incidental untracked dirs in the working checkout.
        st = _git(wt, "status", "--porcelain", "--untracked-files=no", check=False)
        if st.stdout.strip():
            return {"skipped": f"promote repo '{base}' (on '{target}') has uncommitted "
                               "tracked changes; refusing to merge into a dirty tree"}
    else:
        wt = _integration_worktree(settings)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if not wt.exists():
            add = _git(base, "worktree", "add", str(wt), target, check=False)
            if add.returncode != 0:
                # e.g. target checked out in another worktree — surface, don't corrupt.
                return {"skipped": f"cannot create integration worktree on '{target}': "
                                   f"{add.stderr.strip()[:300]}"}
        # Sync the integration worktree to the current target tip before merging.
        _git(wt, "checkout", target, check=False)
        _git(wt, "reset", "--hard", target, check=False)

    if _git(wt, "merge-base", "--is-ancestor", branch, "HEAD",
            check=False).returncode == 0:
        return {"skipped": f"'{branch}' already in '{target}'", "branch": branch}

    merge = _git(wt, "merge", "--no-ff", "-m",
                 f"promote issue #{issue.id}: {branch} -> {target}", branch,
                 check=False)
    if merge.returncode != 0:
        # Auto-heal the common parallel conflict: if the ONLY unmerged path is the root
        # lockfile (package.json merged cleanly), regenerate it instead of bouncing to a human.
        unmerged = _git(wt, "diff", "--name-only", "--diff-filter=U",
                        check=False).stdout.split()
        if unmerged and set(unmerged) <= {"package-lock.json"}:
            healed = _heal_lockfile(wt, issue, target)
            if healed is not None:
                repo.append_log(pool, issue.id, "promoted", healed)
                if settings.auto_rebase_downstream:
                    healed["downstream"] = sync_downstream(pool, issue, settings)
                return healed
        _git(wt, "merge", "--abort", check=False)  # leave the integration tree pristine
        rec = {"promoted": False, "conflict": True, "branch": branch, "target": target,
               "detail": (merge.stdout[-300:] + merge.stderr[-300:]).strip()}
        repo.append_log(pool, issue.id, "promote_conflict", rec)
        # Complete-and-log: the issue still closes; a human merges it after the fact.
        # Notify orch-monitor so it surfaces on the dashboard Fleet page (open queue).
        try:
            repo.create_message(
                pool, from_team=(issue.team or "orchestration"), to_team="orch-monitor",
                subject=f"Promote conflict — issue #{issue.id} not auto-merged to {target}",
                body=(f"Auto-promote hit a MERGE CONFLICT merging branch '{branch}' into "
                      f"'{target}' for issue #{issue.id} ('{(issue.title or '')[:80]}'). The "
                      f"issue still COMPLETED; the merge was aborted (tree left clean). A human "
                      f"must merge it manually: `git merge {branch}` in a {target} worktree, "
                      f"resolve, commit. Detail: {rec['detail'][:280]}"),
                priority="high", issue_id=issue.id, kind="request")
        except Exception:  # noqa: BLE001 — never let notification failure block completion
            pass
        return rec
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    rec = {"promoted": True, "branch": branch, "target": target, "merge_commit": sha}
    repo.append_log(pool, issue.id, "promoted", rec)
    if settings.auto_rebase_downstream:
        rec["downstream"] = sync_downstream(pool, issue, settings)
    return rec


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
