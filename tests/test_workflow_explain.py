"""Tests for orchestrator workflow explain CLI command.

Pure tests (no DB) that exercise the CLI main path with fixture repos.
"""

from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from orchestrator import cli, config
from orchestrator.config import Settings


# --- Fixtures: real git repos for testing ---


def _git_init(tmpdir: Path, initial_files: dict[str, str] | None = None) -> Path:
    """Initialize a primary git repo with optional initial files."""
    repo = tmpdir / "repo"
    repo.mkdir()

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


# --- Tests ---


class TestWorkflowExplain:
    """Test the workflow explain CLI command."""

    def test_builtin_only_profile_exits_zero(self, tmp_path: Path, capsys):
        """A clean builtin-only profile with no custom actions should exit 0."""
        repo = _git_init(tmp_path)

        # Create a minimal workflow.yaml with only builtin actions
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - builtin: node-deps-reconcile
"""
        )

        # Load settings and run the command
        test_settings = Settings()
        args = Namespace(
            instance=None,
            team=None,
            role=None,
        )

        # Monkeypatch load_settings to return our test settings
        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            # Change to repo directory so CWD resolution works
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out
                # Should show the step with the builtin action
                assert "prepare" in out
                assert "node-deps-reconcile" in out
                assert rc == 0  # Clean profile should exit 0
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load

    def test_custom_action_shows_source_and_verdict(self, tmp_path: Path, capsys):
        """A custom action should show source, verdict, and the command."""
        repo = _git_init(tmp_path)

        # Create a workflow with a custom run action
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - run: npm install
"""
        )

        test_settings = Settings(workspace_manifest="")
        args = Namespace(
            instance=None,
            team=None,
            role=None,
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out

                # Should show step, source (repo), verdict (escalate), and command
                assert "prepare" in out
                assert "[repo]" in out  # source
                assert "escalate" in out  # verdict for custom action without approval
                assert "npm install" in out  # the run string
                assert rc == 0  # No deny, so should be 0
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load

    def test_profile_with_warnings_exits_two(self, tmp_path: Path, capsys):
        """A profile with warnings (e.g., malformed repo file) should exit 2."""
        repo = _git_init(tmp_path)

        # Create a broken workflow.yaml (bad YAML)
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - run: npm install
    invalid_yaml: [unclosed
"""
        )

        test_settings = Settings(workspace_manifest="")
        args = Namespace(
            instance=None,
            team=None,
            role=None,
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out

                # Should print a warning
                assert "warning:" in out
                # Exit code 2 because of the warning
                assert rc == 2
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load

    def test_role_specific_actions_shown_with_bracket(self, tmp_path: Path, capsys):
        """Role-specific actions should be shown with [role] bracket."""
        repo = _git_init(tmp_path)

        # Create a workflow with role-specific actions
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
verify:
  qa:
    - run: npm test
"""
        )

        test_settings = Settings(workspace_manifest="")
        args = Namespace(
            instance=None,
            team=None,
            role="qa",
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out

                # Should show verify with [qa] bracket
                assert "verify [qa]" in out
                assert "npm test" in out
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load

    def test_team_resolves_from_verify_worktrees(self, tmp_path: Path, capsys):
        """The --team flag should resolve worktree from settings.verify_worktrees."""
        repo = _git_init(tmp_path)

        # Create a workflow in the repo
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - builtin: node-deps-reconcile
"""
        )

        # Create settings with verify_worktrees pointing to our repo
        test_settings = Settings(
            workspace_manifest="",
            verify_worktrees={"backend": str(repo)},
        )

        args = Namespace(
            instance=None,
            team="backend",
            role=None,
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            rc = cli._cmd_workflow_explain(args, test_settings)
            out = capsys.readouterr().out

            # Should successfully load and process the repo's profile
            assert "prepare" in out
            assert "node-deps-reconcile" in out
            assert rc == 0
        finally:
            cli.load_settings = original_load

    def test_deny_verdict_exits_two(self, tmp_path: Path, capsys):
        """An action with deny verdict should exit code 2."""
        repo = _git_init(tmp_path)

        # Create a workflow with a custom run action
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - run: rm -rf /
"""
        )

        # Create a workspace manifest that denies this exact command
        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text(
            """
permissions:
  deny:
    - "rm -rf /"
"""
        )

        test_settings = Settings(workspace_manifest=str(manifest_path))
        args = Namespace(
            instance=None,
            team=None,
            role=None,
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out

                # Should show deny verdict
                assert "deny" in out
                # Exit code 2 because of the deny
                assert rc == 2
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load

    def test_output_format_one_line_per_action(self, tmp_path: Path, capsys):
        """Output should be one line per action in the profile."""
        repo = _git_init(tmp_path)

        # Create a workflow with multiple actions
        orch_dir = repo / ".orchestrator"
        orch_dir.mkdir(exist_ok=True)
        (orch_dir / "workflow.yaml").write_text(
            """
prepare:
  - run: npm install
verify:
  - builtin: node-deps-reconcile
    on_fail: warn
  - run: npm test
"""
        )

        test_settings = Settings(workspace_manifest="")
        args = Namespace(
            instance=None,
            team=None,
            role=None,
        )

        original_load = cli.load_settings

        def mock_load(instance=None):
            return test_settings

        cli.load_settings = mock_load
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(str(repo))
            try:
                rc = cli._cmd_workflow_explain(args, test_settings)
                out = capsys.readouterr().out

                lines = [line.strip() for line in out.strip().split("\n") if line.strip()]
                # Should have at least 3 action lines (prepare npm install, verify node-deps, verify npm test)
                assert len(lines) >= 3

                # Check for expected patterns
                assert any("prepare" in line and "npm install" in line for line in lines)
                assert any("verify" in line and "@node-deps-reconcile" in line for line in lines)
                assert any("verify" in line and "npm test" in line for line in lines)
            finally:
                os.chdir(old_cwd)
        finally:
            cli.load_settings = original_load
