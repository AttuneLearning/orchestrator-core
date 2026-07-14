"""Reconcile a worktree's node_modules with its package-lock.json before verify/typecheck.

Verify and dev worktrees keep node_modules across git checkout/clean (the verify
runner cleans without -x on purpose, to avoid a full reinstall every cycle). So when
a dependency lands on main and the worktree checks out the new lockfile, its
node_modules is stale — typecheck then fails with spurious "Cannot find module"
errors: a false negative that bounces green work back to dev and toward off_rails.

ensure_deps_current() detects a lockfile change via a self-managed content-hash
sentinel stored under <gitdir>/orch/orch-lock-hash and runs `npm ci` only when it
actually changed. The sentinel lives in the gitdir (not in the worktree tree) because
`git clean -fd` would wipe an untracked in-tree file; node_modules only survived the
old node_modules/.orch-lock-hash location because it's gitignored, but .orch directory
under the gitdir is the proper location.

npm's own node_modules/.package-lock.json is NOT byte-comparable to the root
lockfile (it is a normalized/hidden variant that differs even right after a clean
install), so it cannot serve as the in-sync marker — hence our own hash.

See ADR-DEV (worktree dependency reconciliation) and the memory note
`verify-worktree-stale-node-modules`.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.workflow import sentinel

NPM_CI_TIMEOUT_S = 600


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_deps_current(worktree: str | Path) -> dict[str, Any]:
    """Reinstall node_modules iff package-lock.json changed since the last install.

    Always safe to call. Cheap (one file hash) when nothing changed. Returns a dict:
      checked   – a package-lock.json was present to check
      installed – `npm ci` actually ran this call
      ok        – False ONLY when a reinstall was needed but failed (caller should
                  treat this as a real verify failure, not proceed to typecheck)
      reason    – short human string
      returncode/stderr_tail/duration_s – present when an install ran
    """
    wt = Path(worktree)
    lock = wt / "package-lock.json"
    if not lock.is_file():
        return {"checked": False, "installed": False, "ok": True,
                "reason": "no package-lock.json (non-node worktree)"}

    want = _hash(lock)
    node_modules = wt / "node_modules"

    # Try to get the new sentinel location. If we can't resolve the gitdir (not a git repo),
    # treat sentinel as unreadable and skip the optimization.
    sentinel_path = None
    try:
        sentinel_path = sentinel.sentinel_path(wt, "orch-lock-hash")
    except ValueError:
        # Not a git repo; skip sentinel optimization and always reinstall when lockfile present
        pass

    have = None

    # Try the new sentinel location first
    if sentinel_path is not None and sentinel_path.is_file():
        try:
            have = sentinel_path.read_text().strip()
        except OSError:
            pass

    # Back-compat: if new sentinel missing but old one exists and matches, honor it once
    # then write the new location (no spurious reinstall on upgrade)
    if have is None:
        old_sentinel = node_modules / ".orch-lock-hash"
        if old_sentinel.is_file() and node_modules.is_dir():
            try:
                old_have = old_sentinel.read_text().strip()
                if old_have == want:
                    have = old_have
                    # Write the new sentinel location as part of the upgrade
                    if sentinel_path is not None:
                        try:
                            sentinel.write_sentinel(wt, "orch-lock-hash", want)
                        except (OSError, ValueError):
                            # If we can't write new sentinel, that's ok - we'll write it
                            # on the next successful install
                            pass
            except OSError:
                pass

    if have == want and node_modules.is_dir():
        return {"checked": True, "installed": False, "ok": True,
                "reason": "node_modules matches lockfile"}

    # Stale (or first run): clean install exactly to the lockfile. Never fall back to
    # `npm install` — that would mutate the lockfile and mask real dependency drift.
    try:
        proc = subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=str(wt), capture_output=True, text=True, timeout=NPM_CI_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"checked": True, "installed": False, "ok": False,
                "reason": f"npm ci timed out after {NPM_CI_TIMEOUT_S}s"}

    if proc.returncode != 0:
        return {"checked": True, "installed": False, "ok": False,
                "reason": "npm ci failed (package.json/lock out of sync?)",
                "returncode": proc.returncode, "stderr_tail": proc.stderr[-800:]}

    # Record what we installed against so the next call is a cheap hash compare. The
    # sentinel is only an optimisation — if writing it fails, we just reinstall again.
    if sentinel_path is not None:
        try:
            sentinel.write_sentinel(wt, "orch-lock-hash", want)
        except OSError:
            pass

    return {"checked": True, "installed": True, "ok": True,
            "reason": "reinstalled: lockfile changed since last install",
            "returncode": 0, "stderr_tail": proc.stderr[-400:]}
