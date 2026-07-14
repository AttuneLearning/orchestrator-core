"""Reconcile a worktree's node_modules with its package-lock.json before verify/typecheck.

Verify and dev worktrees keep node_modules across git checkout/clean (the verify
runner cleans without -x on purpose, to avoid a full reinstall every cycle). So when
a dependency lands on main and the worktree checks out the new lockfile, its
node_modules is stale — typecheck then fails with spurious "Cannot find module"
errors: a false negative that bounces green work back to dev and toward off_rails.

ensure_deps_current() detects a lockfile change via a self-managed content-hash
sentinel (node_modules/.orch-lock-hash) and runs `npm ci` only when it actually
changed. npm's own node_modules/.package-lock.json is NOT byte-comparable to the root
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

NPM_CI_TIMEOUT_S = 600
_SENTINEL = ".orch-lock-hash"


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
    sentinel = node_modules / _SENTINEL
    have = sentinel.read_text().strip() if sentinel.is_file() else None
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
    try:
        node_modules.mkdir(exist_ok=True)
        sentinel.write_text(want)
    except OSError:
        pass

    return {"checked": True, "installed": True, "ok": True,
            "reason": "reinstalled: lockfile changed since last install",
            "returncode": 0, "stderr_tail": proc.stderr[-400:]}
