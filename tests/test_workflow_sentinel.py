"""Tests for workflow sentinel change detection.

Tests build real git repos and verify sentinel behavior in both clone shapes:
- Primary clone: .git is a directory
- Linked worktree: .git is a file (gitdir: pointer)

Key invariant: sentinels live under <gitdir>/orch/ and survive `git clean -fd`.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from orchestrator.workflow import sentinel


# Fixtures: helper functions to set up real git repos


def _git_init(tmpdir: Path, initial_files: dict[str, str] | None = None) -> Path:
    """Initialize a primary git repo with optional initial files.

    Args:
        tmpdir: temporary directory path
        initial_files: dict of {filename: content} to create and commit

    Returns:
        Path to the repo root
    """
    repo = tmpdir / "primary_repo"
    repo.mkdir()

    # Configure git locally so commits work
    subprocess.run(
        ["git", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Create and commit initial files if provided
    if initial_files:
        for filename, content in initial_files.items():
            filepath = repo / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
        subprocess.run(
            ["git", "add", "."],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

    return repo


def _git_worktree_add(primary: Path, worktree_dir: Path) -> Path:
    """Create a linked worktree.

    Args:
        primary: path to primary repo
        worktree_dir: where to create the linked worktree

    Returns:
        Path to the linked worktree
    """
    worktree_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir)],
        cwd=str(primary),
        check=True,
        capture_output=True,
    )
    return worktree_dir


class TestResolveGitdir:
    """Test gitdir resolution for both clone shapes."""

    def test_primary_clone_gitdir_is_directory(self, tmp_path: Path) -> None:
        """In a primary clone, .git is a directory."""
        repo = _git_init(tmp_path)
        gitdir = sentinel.resolve_gitdir(repo)
        assert gitdir.is_dir()
        assert gitdir.name == ".git"
        assert gitdir == repo / ".git"

    def test_linked_worktree_gitdir_from_file(self, tmp_path: Path) -> None:
        """In a linked worktree, .git is a file pointing to the worktree's gitdir."""
        primary = _git_init(tmp_path, {"file.txt": "content"})
        worktree = tmp_path / "linked"
        worktree = _git_worktree_add(primary, worktree)

        # Resolve the gitdir from the linked worktree
        gitdir = sentinel.resolve_gitdir(worktree)
        assert gitdir.is_dir()
        # It should point to the worktree-specific gitdir under .git/worktrees/
        assert gitdir == primary / ".git" / "worktrees" / "linked"

    def test_missing_gitdir_raises_error(self, tmp_path: Path) -> None:
        """Raises ValueError when neither .git directory nor file exists."""
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        with pytest.raises(ValueError, match="No .git found"):
            sentinel.resolve_gitdir(not_a_repo)

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        """resolve_gitdir accepts str or Path."""
        repo = _git_init(tmp_path)
        # As string
        gitdir_from_str = sentinel.resolve_gitdir(str(repo))
        # As Path
        gitdir_from_path = sentinel.resolve_gitdir(repo)
        assert gitdir_from_str == gitdir_from_path


class TestSentinelPath:
    """Test sentinel path construction."""

    def test_sentinel_under_gitdir_orch(self, tmp_path: Path) -> None:
        """Sentinel path is under <gitdir>/orch/<name>."""
        repo = _git_init(tmp_path)
        name = "test-sentinel"
        path = sentinel.sentinel_path(repo, name)

        assert path.name == name
        assert "orch" in path.parts
        # Path should be inside .git/orch
        assert (repo / ".git" / "orch" / name) == path

    def test_sentinel_parents_created(self, tmp_path: Path) -> None:
        """Parents of sentinel path are created."""
        repo = _git_init(tmp_path)
        path = sentinel.sentinel_path(repo, "new-sentinel")
        # Parent should exist after call
        assert path.parent.exists()

    def test_sentinel_in_linked_worktree_points_to_worktree_gitdir(
        self, tmp_path: Path
    ) -> None:
        """In linked worktree, sentinel is in worktree's own gitdir/orch."""
        primary = _git_init(tmp_path, {"file.txt": "content"})
        worktree = tmp_path / "linked"
        worktree = _git_worktree_add(primary, worktree)

        path = sentinel.sentinel_path(worktree, "sentinel-name")
        # Should point to the worktree's own gitdir
        assert path == primary / ".git" / "worktrees" / "linked" / "orch" / "sentinel-name"


class TestCurrentDigest:
    """Test digest computation from glob matches."""

    def test_single_file_digest(self, tmp_path: Path) -> None:
        """Digest of single file changes when content changes."""
        repo = _git_init(tmp_path, {"file.txt": "content1"})
        file_path = repo / "file.txt"

        digest1 = sentinel.current_digest(repo, ["file.txt"])
        assert isinstance(digest1, str)
        assert len(digest1) == 64  # sha256 hex is 64 chars

        # Change file content
        file_path.write_text("content2")
        digest2 = sentinel.current_digest(repo, ["file.txt"])
        assert digest2 != digest1

    def test_multiple_file_glob(self, tmp_path: Path) -> None:
        """Digest includes all files matching glob pattern."""
        repo = _git_init(
            tmp_path,
            {
                "package-lock.json": "lock1",
                "yarn.lock": "yarn1",
            },
        )

        digest = sentinel.current_digest(repo, ["*-lock.json", "*.lock"])
        assert len(digest) == 64

        # Change one file
        (repo / "package-lock.json").write_text("lock2")
        digest2 = sentinel.current_digest(repo, ["*-lock.json", "*.lock"])
        assert digest2 != digest

    def test_deterministic_digest(self, tmp_path: Path) -> None:
        """Digest is deterministic (same content -> same digest)."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        digest1 = sentinel.current_digest(repo, ["file.txt"])
        digest2 = sentinel.current_digest(repo, ["file.txt"])
        assert digest1 == digest2

    def test_empty_glob_list_digest(self, tmp_path: Path) -> None:
        """No globs -> deterministic empty digest."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        digest1 = sentinel.current_digest(repo, [])
        digest2 = sentinel.current_digest(repo, [])
        assert digest1 == digest2
        # Both calls return the same hash (empty)
        assert len(digest1) == 64

    def test_no_matching_files_digest(self, tmp_path: Path) -> None:
        """Glob with no matches -> deterministic empty digest."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        digest1 = sentinel.current_digest(repo, ["nonexistent.txt"])
        digest2 = sentinel.current_digest(repo, ["nonexistent.txt"])
        assert digest1 == digest2

    def test_multiple_patterns(self, tmp_path: Path) -> None:
        """Multiple glob patterns are all included in digest."""
        repo = _git_init(
            tmp_path,
            {
                "package.json": "pkg",
                "package-lock.json": "lock",
            },
        )

        digest = sentinel.current_digest(repo, ["package.json", "package-lock.json"])
        assert len(digest) == 64

        # Change one of them
        (repo / "package.json").write_text("pkg2")
        digest2 = sentinel.current_digest(repo, ["package.json", "package-lock.json"])
        assert digest2 != digest

    def test_nested_files_handled(self, tmp_path: Path) -> None:
        """Globs can match nested files."""
        repo = _git_init(
            tmp_path,
            {
                "src/app.ts": "app",
                "src/index.ts": "index",
            },
        )

        digest = sentinel.current_digest(repo, ["src/*.ts"])
        assert len(digest) == 64

        # Change nested file
        (repo / "src/app.ts").write_text("app2")
        digest2 = sentinel.current_digest(repo, ["src/*.ts"])
        assert digest2 != digest

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        """current_digest accepts str or Path for worktree."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        digest_from_str = sentinel.current_digest(str(repo), ["file.txt"])
        digest_from_path = sentinel.current_digest(repo, ["file.txt"])
        assert digest_from_str == digest_from_path


class TestIsStale:
    """Test stale sentinel detection."""

    def test_missing_sentinel_is_stale(self, tmp_path: Path) -> None:
        """Sentinel absent -> stale."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        stale, digest = sentinel.is_stale(repo, ["file.txt"], "missing-sentinel")
        assert stale is True
        assert len(digest) == 64

    def test_matching_sentinel_not_stale(self, tmp_path: Path) -> None:
        """Sentinel present with matching digest -> not stale."""
        repo = _git_init(tmp_path, {"file.txt": "content"})
        name = "test-sentinel"

        # First call: stale (no sentinel)
        stale1, digest = sentinel.is_stale(repo, ["file.txt"], name)
        assert stale1 is True

        # Write the sentinel
        sentinel.write_sentinel(repo, name, digest)

        # Second call: not stale (sentinel matches)
        stale2, _ = sentinel.is_stale(repo, ["file.txt"], name)
        assert stale2 is False

    def test_changed_content_makes_stale(self, tmp_path: Path) -> None:
        """Content change -> new digest -> sentinel stale."""
        repo = _git_init(tmp_path, {"file.txt": "content1"})
        name = "test-sentinel"

        # Initial: write sentinel
        _, digest1 = sentinel.is_stale(repo, ["file.txt"], name)
        sentinel.write_sentinel(repo, name, digest1)

        # Verify not stale
        stale, _ = sentinel.is_stale(repo, ["file.txt"], name)
        assert stale is False

        # Change file
        (repo / "file.txt").write_text("content2")

        # Now stale again
        stale, _ = sentinel.is_stale(repo, ["file.txt"], name)
        assert stale is True

    def test_sentinel_survives_git_clean(self, tmp_path: Path) -> None:
        """Sentinel under gitdir survives `git clean -fd`."""
        repo = _git_init(tmp_path, {"file.txt": "content"})
        name = "persist-sentinel"

        # Create and write sentinel
        _, digest = sentinel.is_stale(repo, ["file.txt"], name)
        sentinel.write_sentinel(repo, name, digest)

        # Create a worktree file to clean
        (repo / "temp-file.txt").write_text("temp")

        # Run git clean
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        # Sentinel should still exist and match
        stale, _ = sentinel.is_stale(repo, ["file.txt"], name)
        assert stale is False

    def test_sentinel_survives_git_clean_in_linked_worktree(
        self, tmp_path: Path
    ) -> None:
        """Sentinel survives git clean even in linked worktree."""
        primary = _git_init(tmp_path, {"file.txt": "content"})
        worktree = tmp_path / "linked"
        worktree = _git_worktree_add(primary, worktree)

        # Copy file to worktree
        (worktree / "file.txt").write_text("content")

        name = "persist-sentinel"

        # Create sentinel from worktree
        _, digest = sentinel.is_stale(worktree, ["file.txt"], name)
        sentinel.write_sentinel(worktree, name, digest)

        # Create a temp file
        (worktree / "temp-file.txt").write_text("temp")

        # Clean the worktree
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )

        # Sentinel should still match
        stale, _ = sentinel.is_stale(worktree, ["file.txt"], name)
        assert stale is False


class TestWriteSentinel:
    """Test sentinel file writing."""

    def test_write_creates_file(self, tmp_path: Path) -> None:
        """write_sentinel creates the file."""
        repo = _git_init(tmp_path, {"file.txt": "content"})
        name = "test-sentinel"
        digest = "abc123def456"

        sentinel.write_sentinel(repo, name, digest)

        path = sentinel.sentinel_path(repo, name)
        assert path.is_file()
        assert path.read_text() == digest

    def test_write_overwrites(self, tmp_path: Path) -> None:
        """write_sentinel overwrites existing file."""
        repo = _git_init(tmp_path, {"file.txt": "content"})
        name = "test-sentinel"

        sentinel.write_sentinel(repo, name, "old")
        sentinel.write_sentinel(repo, name, "new")

        path = sentinel.sentinel_path(repo, name)
        assert path.read_text() == "new"

    def test_write_creates_parents(self, tmp_path: Path) -> None:
        """write_sentinel (via sentinel_path) creates parent dirs."""
        repo = _git_init(tmp_path)
        name = "test-sentinel"

        # Parents should not exist yet
        path = sentinel.sentinel_path(repo, name)
        assert path.parent.exists()

        sentinel.write_sentinel(repo, name, "digest")
        assert path.is_file()

    def test_write_best_effort_ignores_errors(self, tmp_path: Path) -> None:
        """write_sentinel silently ignores OSError."""
        repo = _git_init(tmp_path, {"file.txt": "content"})

        # Make the orch directory read-only to trigger an error
        orch_dir = repo / ".git" / "orch"
        orch_dir.mkdir(parents=True, exist_ok=True)
        orch_dir.chmod(0o444)

        try:
            # Should not raise; best-effort
            sentinel.write_sentinel(repo, "test", "digest")
        finally:
            # Clean up: restore permissions
            orch_dir.chmod(0o755)


class TestIntegration:
    """Integration tests combining multiple functions."""

    def test_full_sentinel_workflow(self, tmp_path: Path) -> None:
        """Full workflow: detect -> write -> skip -> detect change."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})

        globs = ["package-lock.json"]
        name = "orch-lock-hash"

        # Step 1: First check -> stale, no sentinel yet
        stale, digest1 = sentinel.is_stale(repo, globs, name)
        assert stale is True

        # Step 2: Write sentinel
        sentinel.write_sentinel(repo, name, digest1)

        # Step 3: Second check -> not stale
        stale, digest2 = sentinel.is_stale(repo, globs, name)
        assert stale is False
        assert digest1 == digest2

        # Step 4: Change lockfile
        (repo / "package-lock.json").write_text("lock2")

        # Step 5: Check again -> stale with new digest
        stale, digest3 = sentinel.is_stale(repo, globs, name)
        assert stale is True
        assert digest1 != digest3

        # Step 6: Write new sentinel
        sentinel.write_sentinel(repo, name, digest3)

        # Step 7: Check again -> not stale
        stale, digest4 = sentinel.is_stale(repo, globs, name)
        assert stale is False
        assert digest3 == digest4

    def test_multiple_sentinels_independent(self, tmp_path: Path) -> None:
        """Multiple sentinels can coexist independently."""
        repo = _git_init(
            tmp_path,
            {
                "package-lock.json": "lock",
                "uv.lock": "uv",
            },
        )

        # Sentinel 1 for npm
        stale1, digest1 = sentinel.is_stale(repo, ["package-lock.json"], "npm-hash")
        assert stale1 is True
        sentinel.write_sentinel(repo, "npm-hash", digest1)

        # Sentinel 2 for python
        stale2, digest2 = sentinel.is_stale(repo, ["uv.lock"], "py-hash")
        assert stale2 is True
        sentinel.write_sentinel(repo, "py-hash", digest2)

        # Change npm lockfile
        (repo / "package-lock.json").write_text("lock2")

        # npm-hash should be stale, py-hash should not
        stale1, _ = sentinel.is_stale(repo, ["package-lock.json"], "npm-hash")
        stale2, _ = sentinel.is_stale(repo, ["uv.lock"], "py-hash")
        assert stale1 is True
        assert stale2 is False

    def test_primary_and_linked_worktree_separate_sentinels(
        self, tmp_path: Path
    ) -> None:
        """Primary and linked worktree have separate sentinel locations."""
        primary = _git_init(tmp_path, {"file.txt": "content"})
        worktree = tmp_path / "linked"
        worktree = _git_worktree_add(primary, worktree)

        # Copy file to worktree
        (worktree / "file.txt").write_text("content")

        name = "separate-sentinel"

        # Sentinel paths should be different
        primary_path = sentinel.sentinel_path(primary, name)
        worktree_path = sentinel.sentinel_path(worktree, name)
        assert primary_path != worktree_path

        # Write sentinel in worktree
        _, digest_wt = sentinel.is_stale(worktree, ["file.txt"], name)
        sentinel.write_sentinel(worktree, name, digest_wt)

        # Worktree should not be stale
        stale_wt, _ = sentinel.is_stale(worktree, ["file.txt"], name)
        assert stale_wt is False

        # But primary should still be stale (sentinel doesn't exist there)
        stale_primary, _ = sentinel.is_stale(primary, ["file.txt"], name)
        assert stale_primary is True
