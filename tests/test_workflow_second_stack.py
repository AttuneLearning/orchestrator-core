"""End-to-end acceptance tests for the Python workflow adapter (WP-21).

This test suite verifies that a Python project runs through the complete
workflow pipeline (refresh/prepare/verify/cleanup) using the SAME run_step
path as any other stack, with zero engine changes. The key acceptance
criterion: a tmp git repo with uv.lock + FAKE commands (profile overrides
run:) executes successfully and the sentinel detects lockfile changes.

Tests are hermetic: no uv/poetry/network needed; all commands are fake
(touch/false markers).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from orchestrator.workflow import sentinel
from orchestrator.workflow.adapters import detect_stack, get_adapter
from orchestrator.workflow.loader import load_effective
from orchestrator.workflow.models import Profile
from orchestrator.workflow.permissions import Permissions
from orchestrator.workflow.runner import run_step


def _git_init(tmpdir: Path, initial_files: dict[str, str] | None = None) -> Path:
    """Initialize a primary git repo with optional initial files.

    Args:
        tmpdir: temporary directory path
        initial_files: dict of {filename: content} to create and commit

    Returns:
        Path to the repo root
    """
    repo = tmpdir / "py_repo"
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


class TestPythonStackDetection:
    """Test that Python stack is correctly auto-detected."""

    def test_detect_python_from_uv_lock(self, tmp_path: Path) -> None:
        """Detect 'python' stack when uv.lock is present."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})
        assert detect_stack(repo) == "python"

    def test_detect_python_from_poetry_lock(self, tmp_path: Path) -> None:
        """Detect 'python' stack when poetry.lock is present."""
        repo = _git_init(tmp_path, {"poetry.lock": "[package]\nname = \"test\"\n"})
        assert detect_stack(repo) == "python"

    def test_python_adapter_available(self) -> None:
        """Get PythonAdapter for 'python' stack."""
        adapter = get_adapter("python")
        assert adapter is not None
        assert adapter.default_steps()  # Non-empty for python


class TestPythonWorkflowEndToEnd:
    """End-to-end test: Python project runs prepare/verify/cleanup with fake commands."""

    def test_python_workflow_with_fake_commands(self, tmp_path: Path) -> None:
        """Full workflow: auto-detect python, load profile, run steps with fake commands."""
        # Create a git repo with uv.lock and a repo profile override
        initial_files = {
            "uv.lock": "version = 4\n",
            ".orchestrator/workflow.yaml": textwrap.dedent("""\
                prepare:
                  - run: touch prepared.txt
                    on_fail: block
                verify:
                  - run: touch verified.txt
                    on_fail: block
                cleanup:
                  - run: touch cleaned.txt
                    on_fail: block
                """),
        }
        repo = _git_init(tmp_path, initial_files)

        # Auto-detect should pick 'python'
        assert detect_stack(repo) == "python"

        # Create a minimal settings object for load_effective
        settings = MagicMock()
        settings.workspace_manifest = ""

        # Load the effective profile (should merge defaults + repo override)
        profile = load_effective(settings, repo)
        assert profile.stack == "python"

        # Verify the repo override took effect (prepare has our fake run command)
        prepare_step = profile.step("prepare")
        assert len(prepare_step.actions) == 1
        assert prepare_step.actions[0].run == "touch prepared.txt"

        # Build permissions with bypass=True to allow custom commands
        perms = Permissions(bypass=True)

        # Run each step
        # Prepare
        prepare_result = run_step(repo, profile, "prepare", role="qa", perms=perms)
        assert prepare_result.status == "ok", f"prepare failed: {prepare_result.reason}"
        assert (repo / "prepared.txt").exists(), "prepare didn't create marker"

        # Verify
        verify_result = run_step(repo, profile, "verify", role="qa", perms=perms)
        assert verify_result.status == "ok", f"verify failed: {verify_result.reason}"
        assert (repo / "verified.txt").exists(), "verify didn't create marker"

        # Cleanup (fake cleanup that creates a marker instead of real reset/clean)
        cleanup_result = run_step(repo, profile, "cleanup", role="qa", perms=perms)
        assert cleanup_result.status == "ok", f"cleanup failed: {cleanup_result.reason}"
        assert (repo / "cleaned.txt").exists(), "cleanup didn't create marker"

    def test_python_prepare_builtin_no_lockfile(self, tmp_path: Path) -> None:
        """py-deps-reconcile builtin returns ok=True when no lockfile exists."""
        repo = _git_init(tmp_path, {})  # No uv.lock or poetry.lock

        adapter = get_adapter("python")
        builtins_dict = adapter.builtins()
        builtin_fn = builtins_dict["py-deps-reconcile"]

        result = builtin_fn(repo)
        assert result["ok"] is True
        assert "no python lockfile" in result["reason"]

    def test_python_prepare_builtin_with_uv_lock(self, tmp_path: Path) -> None:
        """py-deps-reconcile detects uv.lock and would run uv sync."""
        # Create git repo with uv.lock
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\nresolution-markers = []\n"})

        adapter = get_adapter("python")
        builtins_dict = adapter.builtins()
        builtin_fn = builtins_dict["py-deps-reconcile"]

        # Mock subprocess.run to avoid actual uv invocation
        with patch("orchestrator.workflow.adapters.python.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            result = builtin_fn(repo)
            assert result["ok"] is True
            assert result["installed"] is True
            assert "reinstalled" in result["reason"]
            # Verify uv sync was called
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0][0] == ["uv", "sync", "--frozen"]


class TestSentinelDetectsLockfileChange:
    """Test that the py-lock-hash sentinel correctly detects lockfile changes."""

    def test_sentinel_skips_unchanged_lockfile(self, tmp_path: Path) -> None:
        """Sentinel: first run installs, second run (unchanged lock) skips."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\nfirst-content\n"})

        adapter = get_adapter("python")
        builtins_dict = adapter.builtins()
        builtin_fn = builtins_dict["py-deps-reconcile"]

        # Mock subprocess to track calls
        with patch("orchestrator.workflow.adapters.python.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            # First run: should call uv sync
            result1 = builtin_fn(repo)
            assert result1["ok"] is True
            assert result1["installed"] is True
            assert mock_run.call_count == 1

            # Simulate successful install by creating .venv directory
            venv = repo / ".venv"
            venv.mkdir(parents=True, exist_ok=True)

            # Second run with same content: should NOT call uv sync (detected as unchanged)
            result2 = builtin_fn(repo)
            assert result2["ok"] is True
            assert result2["installed"] is False  # Not reinstalled
            assert "matches" in result2["reason"]
            # Should still be 1 call (no new call for second run)
            assert mock_run.call_count == 1

    def test_sentinel_detects_lockfile_change(self, tmp_path: Path) -> None:
        """Sentinel: changing lockfile content triggers reinstall."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\nfirst-content\n"})

        adapter = get_adapter("python")
        builtins_dict = adapter.builtins()
        builtin_fn = builtins_dict["py-deps-reconcile"]

        with patch("orchestrator.workflow.adapters.python.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            # First run: install
            result1 = builtin_fn(repo)
            assert result1["installed"] is True
            assert mock_run.call_count == 1

            # Simulate successful install by creating .venv directory
            venv = repo / ".venv"
            venv.mkdir(parents=True, exist_ok=True)

            # Change the lockfile
            lockfile = repo / "uv.lock"
            lockfile.write_text("version = 4\nsecond-content\n")

            # Second run: should detect change and reinstall
            result2 = builtin_fn(repo)
            assert result2["installed"] is True
            assert "changed" in result2["reason"].lower()
            # Should be 2 calls now (new call for lockfile change)
            assert mock_run.call_count == 2

    def test_sentinel_survives_git_clean(self, tmp_path: Path) -> None:
        """Sentinel lives under gitdir and survives git clean -fd."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})

        # Manually compute and write a sentinel
        from orchestrator.workflow import sentinel as sentinel_mod

        globs = ["uv.lock"]
        stale, digest = sentinel_mod.is_stale(repo, globs, "py-lock-hash")
        assert stale is True  # First time, always stale

        sentinel_mod.write_sentinel(repo, "py-lock-hash", digest)
        sent_path = sentinel_mod.sentinel_path(repo, "py-lock-hash")
        assert sent_path.is_file(), "sentinel should be written"

        # Run git clean -fd in the worktree
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        # Sentinel should still exist (it's under gitdir, not worktree)
        assert sent_path.is_file(), "sentinel should survive git clean"

        # Verify is_stale detects it as fresh (not stale)
        stale2, digest2 = sentinel_mod.is_stale(repo, globs, "py-lock-hash")
        assert stale2 is False, "sentinel should mark lock as fresh"
        assert digest == digest2, "digest should be same"

    def test_venv_guard_reinstalls_when_missing(self, tmp_path: Path) -> None:
        """venv-existence guard: reinstall when .venv is missing even if sentinel matches."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})

        adapter = get_adapter("python")
        builtins_dict = adapter.builtins()
        builtin_fn = builtins_dict["py-deps-reconcile"]

        with patch("orchestrator.workflow.adapters.python.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            # First run: install and create sentinel
            result1 = builtin_fn(repo)
            assert result1["ok"] is True
            assert result1["installed"] is True
            assert mock_run.call_count == 1

            # Simulate .venv being deleted (e.g., by a CI cleanup or developer action)
            venv = repo / ".venv"
            # (we don't create it, it doesn't exist)
            assert not venv.is_dir(), "venv should not exist yet"

            # Second run with same lockfile but missing venv: should reinstall
            result2 = builtin_fn(repo)
            assert result2["ok"] is True
            assert result2["installed"] is True  # Reinstalled due to missing venv
            assert "changed" in result2["reason"].lower() or "stale" in result2["reason"].lower()
            # Should be 2 calls now (new call due to missing venv)
            assert mock_run.call_count == 2


class TestPythonWorkflowViaRunStep:
    """Test Python workflow steps via run_step with mocked builtins."""

    def test_prepare_with_fake_builtin(self, tmp_path: Path) -> None:
        """Prepare step executes the py-deps-reconcile builtin."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})

        # Load profile (should auto-apply python adapter defaults)
        settings = MagicMock()
        settings.workspace_manifest = ""
        profile = load_effective(settings, repo)
        assert profile.stack == "python"

        # Mock the builtin to avoid actual uv invocation
        with patch("orchestrator.workflow.adapters.python._py_deps_reconcile") as mock_builtin:
            mock_builtin.return_value = {"ok": True, "reason": "mocked", "installed": True}

            perms = Permissions()
            result = run_step(repo, profile, "prepare", role="qa", perms=perms)
            assert result.status == "ok", f"prepare failed: {result.reason}"
            # Builtin should have been called
            mock_builtin.assert_called_once()

    def test_verify_runs_pytest(self, tmp_path: Path) -> None:
        """Verify step defaults to running python -m pytest -q."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})

        settings = MagicMock()
        settings.workspace_manifest = ""
        profile = load_effective(settings, repo)

        # Check the default verify action
        verify_step = profile.step("verify")
        assert len(verify_step.actions) == 1
        assert verify_step.actions[0].run == "python -m pytest -q"

    def test_cleanup_runs_git_reset_clean(self, tmp_path: Path) -> None:
        """Cleanup step defaults to git reset --hard && git clean -fd."""
        repo = _git_init(tmp_path, {"uv.lock": "version = 4\n"})

        settings = MagicMock()
        settings.workspace_manifest = ""
        profile = load_effective(settings, repo)

        # Check the default cleanup action
        cleanup_step = profile.step("cleanup")
        assert len(cleanup_step.actions) == 1
        assert cleanup_step.actions[0].run == "git reset --hard && git clean -fd"
