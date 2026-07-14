"""Deterministic change detection via gitdir-resident sentinels.

Workflow steps can include a `sentinel` to avoid re-running actions when their
dependencies haven't changed. Sentinels are stored under <gitdir>/orch/<name>
(never in the worktree tree), so they survive `git clean -fd` and `git checkout`.

Change detection works via a composite digest of all files matching the action's
`when_changed` glob patterns (relative to worktree root). The digest is
deterministic (sorted paths, sha256 over content) and changes when any matched
file's content changes.

See WP-02 (Workflow Profile system) and the hard rule: sentinels never live
inside the worktree tree.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def resolve_gitdir(worktree: str | Path) -> Path:
    """Real gitdir of a checkout.

    .git is a DIRECTORY in a primary clone but a FILE in a linked worktree
    (containing one line: 'gitdir: /abs/path/to/real/gitdir'). Handle both.

    Args:
        worktree: path to the checkout root

    Returns:
        Path to the real .git directory (always a directory, never a file)

    Raises:
        ValueError: if neither .git directory nor .git file exists
    """
    wt = Path(worktree)
    git_path = wt / ".git"

    if git_path.is_dir():
        # Primary clone: .git is a directory
        return git_path

    if git_path.is_file():
        # Linked worktree: .git is a file containing "gitdir: /path/to/gitdir"
        content = git_path.read_text()
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("gitdir:"):
                return Path(line[len("gitdir:"):].strip())

    raise ValueError(
        f"No .git found at {wt} (neither directory nor gitdir file)"
    )


def sentinel_path(worktree: str | Path, name: str) -> Path:
    """Path where a sentinel file is stored.

    Sentinels live under <gitdir>/orch/ so they survive git clean/checkout.
    Parent directories are created if needed.

    Args:
        worktree: path to the checkout root
        name: sentinel name (e.g., "orch-lock-hash")

    Returns:
        Path to the sentinel file (under <gitdir>/orch/<name>)
    """
    gitdir = resolve_gitdir(worktree)
    orch_dir = gitdir / "orch"
    orch_dir.mkdir(parents=True, exist_ok=True)
    return orch_dir / name


def current_digest(worktree: str | Path, globs: Sequence[str]) -> str:
    """Deterministic digest of files matching glob patterns.

    Computes sha256 over a sorted list of (relpath, file_content_sha256) tuples
    for all files matching any of the glob patterns (relative to worktree root).

    Args:
        worktree: path to the checkout root
        globs: sequence of glob patterns (relative to worktree root),
               e.g., ["package-lock.json", "*.yaml"]

    Returns:
        sha256 hexdigest of the composite (empty digest if no matches)
    """
    wt = Path(worktree)
    files: dict[str, str] = {}  # relpath -> file_sha256

    # Collect all matching files
    for glob_pattern in globs:
        for match in wt.glob(glob_pattern):
            if match.is_file():
                relpath = str(match.relative_to(wt))
                # Compute sha256 of the file content
                content_hash = hashlib.sha256(match.read_bytes()).hexdigest()
                files[relpath] = content_hash

    # Sort by path for determinism, then concatenate
    sorted_pairs = [(relpath, files[relpath]) for relpath in sorted(files.keys())]

    # If no files matched, digest the empty list
    if not sorted_pairs:
        return hashlib.sha256(b"[]").hexdigest()

    # Digest the sorted list as a string representation
    pairs_str = str(sorted_pairs)
    return hashlib.sha256(pairs_str.encode("utf-8")).hexdigest()


def is_stale(worktree: str | Path, globs: Sequence[str], name: str) -> tuple[bool, str]:
    """Check if a sentinel is stale.

    Returns (True, new_digest) when sentinel is absent or digest mismatch,
    indicating the action should be re-run. Returns (False, digest) when
    sentinel matches, indicating the action can be skipped.

    Args:
        worktree: path to the checkout root
        globs: glob patterns for dependency files
        name: sentinel name

    Returns:
        (stale: bool, current_digest: str) tuple
    """
    new_digest = current_digest(worktree, globs)
    sentinel = sentinel_path(worktree, name)

    if not sentinel.is_file():
        # Sentinel doesn't exist; always stale
        return (True, new_digest)

    try:
        old_digest = sentinel.read_text().strip()
    except OSError:
        # Error reading sentinel; treat as stale
        return (True, new_digest)

    if old_digest == new_digest:
        return (False, new_digest)

    return (True, new_digest)


def write_sentinel(worktree: str | Path, name: str, digest: str) -> None:
    """Write a sentinel file.

    Best-effort: if the write fails (OSError), silently ignore and continue.
    The next run will see the sentinel as missing and re-run the action.

    Args:
        worktree: path to the checkout root
        name: sentinel name
        digest: the digest string to write
    """
    sentinel = sentinel_path(worktree, name)
    try:
        sentinel.write_text(digest)
    except OSError:
        # Ignore write failures; next call will treat as stale
        pass
