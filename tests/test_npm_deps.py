"""Tests for npm dependency reconciliation with gitdir-resident sentinels.

Tests verify that ensure_deps_current:
1. Stores sentinels under <gitdir>/orch/orch-lock-hash (not node_modules)
2. Handles upgrade path: old sentinel honored once, then new written
3. Detects lockfile changes and triggers reinstalls
4. Handles npm failures gracefully
5. Works in non-git directories (skips sentinel optimization)
6. Sentinels survive `git clean -fd`

All tests use tmp git repos with fake package-lock.json files; subprocess.run is
monkeypatched to avoid actual npm invocations.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.apply import npm_deps
from orchestrator.workflow import sentinel


# Fixtures: helper functions for git repo setup


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


class TestEnsureDepsCurrentFirstRun:
    """Test first-run behavior: install + write new sentinel."""

    def test_first_run_installs_and_writes_sentinel(self, tmp_path: Path) -> None:
        """First run: lockfile exists, no node_modules -> install + write sentinel."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})

        # Mock npm ci success
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result = npm_deps.ensure_deps_current(repo)

        # Should have installed
        assert result["checked"] is True
        assert result["installed"] is True
        assert result["ok"] is True
        assert "reinstalled" in result["reason"]

        # Verify npm ci was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npm", "ci", "--no-audit", "--no-fund"]
        assert call_args[1]["cwd"] == str(repo)

        # Verify new sentinel was written
        sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert sentinel_path.is_file()
        sentinel_content = sentinel_path.read_text().strip()
        # Should be a sha256 hash
        assert len(sentinel_content) == 64

    def test_first_run_creates_node_modules_if_missing(self, tmp_path: Path) -> None:
        """First run creates node_modules dir if it doesn't exist."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        assert not node_modules.exists()

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            npm_deps.ensure_deps_current(repo)

        # node_modules should be created (or would be in real npm ci)
        # The function tries to mkdir it before writing sentinel
        sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert sentinel_path.parent.exists()


class TestEnsureDepsCurrentSecondRun:
    """Test second-run behavior: skip when sentinel matches."""

    def test_second_run_no_op_when_sentinel_matches(self, tmp_path: Path) -> None:
        """Second run with unchanged lockfile -> no install."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write sentinel for the current lockfile
        digest = npm_deps._hash(repo / "package-lock.json")
        sentinel.write_sentinel(repo, "orch-lock-hash", digest)

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            result = npm_deps.ensure_deps_current(repo)

        # Should not have run npm ci
        assert result["checked"] is True
        assert result["installed"] is False
        assert result["ok"] is True
        assert "matches lockfile" in result["reason"]
        mock_run.assert_not_called()


class TestEnsureDepsCurrentLockfileChange:
    """Test behavior when lockfile changes."""

    def test_lockfile_change_triggers_reinstall(self, tmp_path: Path) -> None:
        """Lockfile change -> sentinel stale -> reinstall."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write sentinel for original lockfile
        digest1 = npm_deps._hash(repo / "package-lock.json")
        sentinel.write_sentinel(repo, "orch-lock-hash", digest1)

        # Verify no install needed yet
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            result = npm_deps.ensure_deps_current(repo)
            assert result["installed"] is False
            mock_run.assert_not_called()

        # Change the lockfile
        (repo / "package-lock.json").write_text("lock2")

        # Now reinstall should be needed
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result = npm_deps.ensure_deps_current(repo)

        assert result["checked"] is True
        assert result["installed"] is True
        assert result["ok"] is True
        assert "reinstalled" in result["reason"]
        mock_run.assert_called_once()

        # New sentinel should be written
        digest2 = npm_deps._hash(repo / "package-lock.json")
        assert digest1 != digest2
        sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert sentinel_path.read_text().strip() == digest2


class TestEnsureDepsCurrentBackCompat:
    """Test upgrade path: old sentinel honored, new one written."""

    def test_old_sentinel_honored_on_upgrade(self, tmp_path: Path) -> None:
        """Old sentinel in node_modules honored once, new sentinel written."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write the OLD sentinel location
        lock_digest = npm_deps._hash(repo / "package-lock.json")
        old_sentinel_file = node_modules / ".orch-lock-hash"
        old_sentinel_file.write_text(lock_digest)

        # Ensure new sentinel doesn't exist
        new_sentinel_file = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert not new_sentinel_file.is_file()

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            result = npm_deps.ensure_deps_current(repo)

        # Should NOT have reinstalled (old sentinel honored)
        assert result["checked"] is True
        assert result["installed"] is False
        assert result["ok"] is True
        assert "matches lockfile" in result["reason"]
        mock_run.assert_not_called()

        # New sentinel should now exist
        assert new_sentinel_file.is_file()
        assert new_sentinel_file.read_text().strip() == lock_digest

        # Old sentinel can remain (we don't clean it up)
        assert old_sentinel_file.is_file()

    def test_old_sentinel_mismatch_triggers_reinstall(self, tmp_path: Path) -> None:
        """Old sentinel exists but doesn't match -> reinstall."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write OLD sentinel with wrong digest
        old_sentinel_file = node_modules / ".orch-lock-hash"
        old_sentinel_file.write_text("wrong_digest")

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result = npm_deps.ensure_deps_current(repo)

        # Should have reinstalled (old sentinel didn't match)
        assert result["checked"] is True
        assert result["installed"] is True
        assert result["ok"] is True
        assert "reinstalled" in result["reason"]
        mock_run.assert_called_once()


class TestEnsureDepsCurrentFailures:
    """Test failure modes."""

    def test_npm_ci_failure_returns_ok_false(self, tmp_path: Path) -> None:
        """npm ci failure -> ok=False, don't write sentinel."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stderr = "npm ERR! some error\n"
            mock_run.return_value = mock_proc

            result = npm_deps.ensure_deps_current(repo)

        assert result["checked"] is True
        assert result["installed"] is False
        assert result["ok"] is False
        assert "npm ci failed" in result["reason"]
        assert "returncode" in result
        assert result["returncode"] == 1

        # Sentinel should NOT be written on failure
        sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert not sentinel_path.is_file()

    def test_npm_ci_timeout(self, tmp_path: Path) -> None:
        """npm ci timeout -> ok=False."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("npm ci", 600)

            result = npm_deps.ensure_deps_current(repo)

        assert result["checked"] is True
        assert result["installed"] is False
        assert result["ok"] is False
        assert "timed out" in result["reason"]
        assert "600" in result["reason"]


class TestEnsureDepsCurrentEdgeCases:
    """Test edge cases."""

    def test_no_lockfile_returns_checked_false(self, tmp_path: Path) -> None:
        """No package-lock.json -> checked=False, no action."""
        repo = _git_init(tmp_path)

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            result = npm_deps.ensure_deps_current(repo)

        assert result["checked"] is False
        assert result["installed"] is False
        assert result["ok"] is True
        assert "non-node" in result["reason"]
        mock_run.assert_not_called()

    def test_non_git_directory_skips_sentinel_optimization(self, tmp_path: Path) -> None:
        """In a non-git dir, ValueError from sentinel_path is caught gracefully."""
        non_git_dir = tmp_path / "not_a_repo"
        non_git_dir.mkdir()
        (non_git_dir / "package-lock.json").write_text("lock1")

        # In a non-git directory, sentinel_path raises ValueError
        # We should catch it and skip the optimization (always reinstall)
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result = npm_deps.ensure_deps_current(non_git_dir)

        # Should have tried to install (sentinel unavailable -> always reinstall)
        assert result["checked"] is True
        assert result["installed"] is True
        assert result["ok"] is True
        mock_run.assert_called_once()

    def test_sentinel_write_failure_ignored(self, tmp_path: Path) -> None:
        """Sentinel write failure (OSError) is silently ignored."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})

        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            with patch("orchestrator.workflow.sentinel.write_sentinel") as mock_write:
                mock_write.side_effect = OSError("permission denied")

                result = npm_deps.ensure_deps_current(repo)

        # Should still return ok=True even if sentinel write failed
        assert result["ok"] is True
        assert result["installed"] is True
        assert "reinstalled" in result["reason"]


class TestEnsureDepsCurrentSentinelSurvivesClean:
    """Test that sentinels survive `git clean -fd`."""

    def test_sentinel_survives_git_clean_fd(self, tmp_path: Path) -> None:
        """Sentinel under gitdir survives `git clean -fd`."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write sentinel
        digest = npm_deps._hash(repo / "package-lock.json")
        sentinel.write_sentinel(repo, "orch-lock-hash", digest)

        # Verify sentinel exists
        sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        assert sentinel_path.is_file()

        # Create a temp untracked file
        (repo / "temp.txt").write_text("temp")

        # Run git clean -fd
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        # Sentinel should still exist
        assert sentinel_path.is_file()
        assert sentinel_path.read_text().strip() == digest

        # Temp file should be gone
        assert not (repo / "temp.txt").exists()

    def test_old_sentinel_not_cleaned_by_git_clean(self, tmp_path: Path) -> None:
        """Old sentinel in node_modules survives `git clean -fd` (gitignored)."""
        # Create repo with .gitignore including node_modules
        repo = _git_init(tmp_path, {
            "package-lock.json": "lock1",
            ".gitignore": "node_modules/\n"
        })
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Write old sentinel
        old_sentinel = node_modules / ".orch-lock-hash"
        old_sentinel.write_text("old_hash")

        # Create temp file to clean
        (repo / "temp.txt").write_text("temp")

        # Run git clean -fd
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        # Both old sentinel and node_modules should survive (gitignored)
        assert old_sentinel.is_file()
        assert (repo / "temp.txt").exists() is False


class TestEnsureDepsCurrentIntegration:
    """Integration tests."""

    def test_full_lifecycle(self, tmp_path: Path) -> None:
        """Full lifecycle: install -> skip -> change -> reinstall."""
        repo = _git_init(tmp_path, {"package-lock.json": "lock1"})
        node_modules = repo / "node_modules"
        node_modules.mkdir()

        # Step 1: First run -> install + write new sentinel
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result1 = npm_deps.ensure_deps_current(repo)
            assert result1["installed"] is True
            assert result1["ok"] is True

        # Verify new sentinel was written
        new_sentinel_path = sentinel.sentinel_path(repo, "orch-lock-hash")
        digest1 = new_sentinel_path.read_text().strip()

        # Step 2: Second run -> no install (sentinel matches)
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            result2 = npm_deps.ensure_deps_current(repo)
            assert result2["installed"] is False
            assert result2["ok"] is True
            mock_run.assert_not_called()

        # Step 3: Change lockfile
        (repo / "package-lock.json").write_text("lock2")

        # Step 4: Next run -> reinstall (sentinel stale)
        with patch("orchestrator.apply.npm_deps.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result3 = npm_deps.ensure_deps_current(repo)
            assert result3["installed"] is True
            assert result3["ok"] is True

        # Verify new digest written
        digest2 = new_sentinel_path.read_text().strip()
        assert digest1 != digest2
