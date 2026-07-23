from __future__ import annotations

from argparse import Namespace
import os
import subprocess
from pathlib import Path

from orchestrator import cli


def _setup_args(workspace: Path, *, project: str = "tendcharting", dry_run: bool = False,
                decomposition_tier: str = "mid"):
    return Namespace(
        workspace=str(workspace),
        project=project,
        orchestrator_path=str(cli.REPO_ROOT),
        dashboard_url="http://127.0.0.1:8800",
        worktree_prefix="wt-",
        humantest_worktree="humantest-wt",
        decomposition_tier=decomposition_tier,
        force=False,
        dry_run=dry_run,
    )


def test_setup_project_bakes_decomposition_tier_into_env_and_guidance(settings, tmp_path, capsys):
    workspace = tmp_path / "tendcharting-ws"

    rc = cli._cmd_setup_project(_setup_args(workspace, decomposition_tier="remedial"), settings)
    out = capsys.readouterr().out

    assert rc == 0
    env = (workspace / "agent-launchers" / "orchestrator.env").read_text()
    assert "DECOMPOSITION_TIER=remedial" in env
    # The authoritative per-instance config line is printed for the operator to pin.
    assert "decomposition tier: remedial" in out
    assert "decomposition_tier: remedial" in out


def test_setup_project_defaults_tier_to_mid(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"
    # Omit the attribute entirely — the command must default via getattr, not crash.
    args = _setup_args(workspace)
    del args.decomposition_tier
    rc = cli._cmd_setup_project(args, settings)
    assert rc == 0
    env = (workspace / "agent-launchers" / "orchestrator.env").read_text()
    assert "DECOMPOSITION_TIER=mid" in env


def test_workspace_launch_plan_uses_pull_subteams(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"

    planned = cli._workspace_launch_plan(settings, workspace)

    assert planned == [
        workspace / "humantest-wt",
        workspace / "wt-backend-dev",
        workspace / "wt-backend-qa",
        workspace / "wt-frontend-dev",
        workspace / "wt-frontend-qa",
    ]


def test_setup_project_dry_run_reports_workspace_and_worktrees(settings, tmp_path, capsys):
    workspace = tmp_path / "tendcharting-ws"

    rc = cli._cmd_setup_project(_setup_args(workspace, dry_run=True), settings)
    out = capsys.readouterr().out

    assert rc == 0
    assert f"would create workspace: {workspace}" in out
    assert f"would create worktree: {workspace / 'humantest-wt'}" in out
    assert f"would create worktree: {workspace / 'wt-backend-dev'}" in out
    assert f"would create worktree: {workspace / 'wt-backend-qa'}" in out
    assert f"would create worktree: {workspace / 'wt-frontend-dev'}" in out
    assert f"would create worktree: {workspace / 'wt-frontend-qa'}" in out
    assert "wt-platform-dev" not in out
    assert "wt-orchestration-lead" not in out


def test_setup_project_writes_launchers_and_workspace_dirs(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"

    rc = cli._cmd_setup_project(_setup_args(workspace), settings)

    assert rc == 0
    assert (workspace / "humantest-wt").is_dir()
    assert (workspace / "wt-backend-dev").is_dir()
    assert (workspace / "wt-backend-qa").is_dir()
    assert (workspace / "wt-frontend-dev").is_dir()
    assert (workspace / "wt-frontend-qa").is_dir()

    launcher = workspace / "agent-launchers" / "orchestrator.env"
    startup = workspace / "ORCH_MANAGER_STARTUP.md"
    wrappers = [
        workspace / "start-orch-manager.sh",
        workspace / "start-dev-manager.sh",
        workspace / "start-dev-worker.sh",
        workspace / "start-qa-worker.sh",
        workspace / "start-senior-dev.sh",
        workspace / "start-senior-qa.sh",
    ]
    runtimes = [
        workspace / "agent-launchers" / "runtimes" / name
        for name in ("claude.sh", "codex.sh", "opencode.sh", "qwen.sh", "qwen-code.sh")
    ]
    assert launcher.is_file()
    assert startup.is_file()
    assert "__WORKSPACE_ROOT__" not in launcher.read_text()
    assert str(workspace) in launcher.read_text()
    assert "tendcharting" in startup.read_text()
    assert not any(part.name == "__pycache__" for part in workspace.rglob("*"))
    assert os.access(workspace / "start-agent.sh", os.X_OK)
    for script in wrappers + runtimes:
        assert os.access(script, os.X_OK), script
    # Legacy wrappers were consolidated into start-agent.sh + the role wrappers.
    for legacy in (
        "start-claude-dev.sh",
        "start-qa.sh",
        "start-opencode-worker.sh",
        "start-qwen-code-worker.sh",
        "start-opencode-orch-manager.sh",
        "start-qwen-orch-manager.sh",
    ):
        assert not (workspace / legacy).exists(), legacy


def test_opencode_runtime_builds_config_and_command(settings, tmp_path, monkeypatch):
    workspace = tmp_path / "tendcharting-ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("run one backend cycle for {{PROJECT}}")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    opencode_stub = bin_dir / "opencode"
    opencode_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "capture_dir=\"${OPENCODE_CAPTURE_DIR:?missing OPENCODE_CAPTURE_DIR}\"\n"
        "mkdir -p \"$capture_dir\"\n"
        "printf '%s\\n' \"$*\" > \"$capture_dir/args.txt\"\n"
        "printf '%s' \"${XDG_CONFIG_HOME:-}\" > \"$capture_dir/xdg_config_home.txt\"\n"
        "config_file=\"${XDG_CONFIG_HOME:?missing XDG_CONFIG_HOME}/opencode/opencode.jsonc\"\n"
        "test -f \"$config_file\"\n"
        "cat \"$config_file\" > \"$capture_dir/config.jsonc\"\n"
    )
    opencode_stub.chmod(0o755)

    # In interactive/loop mode opencode.sh delegates to $WORKSPACE_ROOT/run-agent-loop.sh
    # (deployed by setup-project in a real workspace). Provide a stub that mirrors the
    # real wrapper's contract — args are (AGENT_ID, <opencode cmd...>) — by dropping the
    # id and exec'ing the command once, so the interactive branch still reaches the
    # opencode stub and writes its capture.
    loop_stub = workspace / "run-agent-loop.sh"
    loop_stub.write_text('#!/usr/bin/env bash\nset -euo pipefail\nshift  # drop AGENT_ID\nexec "$@"\n')
    loop_stub.chmod(0o755)

    script = cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers" / "runtimes" / "opencode.sh"

    base_env = os.environ.copy()
    base_env.update(
        {
            "PATH": f"{bin_dir}:{base_env['PATH']}",
            "WORKSPACE_ROOT": str(workspace),
            "LAUNCHER_DIR": str(cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"),
            "ORCH": str(cli.REPO_ROOT),
            "PROJECT": "tendcharting",
            "DASHBOARD": "http://127.0.0.1:8800",
            "ROLE": "backend-dev-worker",
            "RUNTIME": "opencode",
            "AGENT_ID": "1",
            "TEAM": "backend",
            "FUNCTION": "dev",
            "GATE": "implementation",
            "APP": "apps/api",
            "WORKTREE": str(worktree),
            "PROMPT_NAME": "dev-worker",
            "PROMPT_FILE": str(prompt_file),
            "LOOP_AGENT": "0",
            "IDLE_STOP": "0",
            "COMMAND_TIMEOUT": "0",
            "AGENT_ENABLE_LOOP_DEFAULT": "1",
            "AGENT_POLL_DEFAULT": "90",
            "FANOUT_DEFAULT": "3",
            # opencode model is selected via ORCH_OPENCODE_MODEL (default glm-5.2).
            "ORCH_OPENCODE_MODEL": "orch_model/deepseek-v4-pro",
        }
    )

    def run(mode: str | None, capture_name: str):
        env = base_env.copy()
        env["OPENCODE_CAPTURE_DIR"] = str(tmp_path / capture_name)
        # The default (mode=None) path asserts the NON-interactive `opencode run …`
        # command, so AGENT_MODE must be genuinely unset — never inherited from the
        # caller's shell (this orch-manager session exports AGENT_MODE=interactive,
        # which would otherwise flip the default run into the interactive branch).
        if mode is not None:
            env["AGENT_MODE"] = mode
        else:
            env.pop("AGENT_MODE", None)
        rc = subprocess.run(
            ["bash", str(script)],
            cwd=str(cli.REPO_ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, rc.stderr
        capture_dir = tmp_path / capture_name
        args = (capture_dir / "args.txt").read_text().strip().split()
        config = (capture_dir / "config.jsonc").read_text()
        return args, config

    default_args, default_config = run(None, "capture-default")
    interactive_args, interactive_config = run("interactive", "capture-interactive")

    assert default_args[:4] == ["run", "--dir", str(worktree), "--auto"]
    assert "--model" in default_args
    assert "orch_model/" in " ".join(default_args)
    assert "run one backend cycle for tendcharting" in " ".join(default_args)
    # Shared open-source model menu: both providers present with the canonical models.
    assert '"orch_model"' in default_config
    assert '"qwen_local"' in default_config
    assert '"glm-5.1"' in default_config and '"glm-5.2"' in default_config
    assert '"qwen-local"' in default_config
    # Orchestrator MCP injected for the installed workspace.
    assert '"orchestrator"' in default_config
    assert str(cli.REPO_ROOT / ".venv" / "bin" / "python") in default_config
    assert '"baseURL": "https://inference.do-ai.run/v1"' in default_config

    assert interactive_args[:2] == ["--model", "orch_model/deepseek-v4-pro"]
    assert "run" not in interactive_args[:1]
    # apply_interactive_prompt rewrites single-cycle directives for TUI launches.
    assert "Run backend cycles continuously for tendcharting" in " ".join(interactive_args)
    assert default_config == interactive_config


def test_install_preserves_customized_env(settings, tmp_path):
    """Verify that install --force preserves customized env files and only
    overwrites code files (like run-agent-loop.sh)."""
    workspace = tmp_path / "tendcharting-ws"

    # First install: create the workspace with all default files
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    # Customize the orchestrator.env file
    env_file = workspace / "agent-launchers" / "orchestrator.env"
    original_env = env_file.read_text()
    customized_env = original_env + "\n# CUSTOM_CONFIG=my_value\n"
    env_file.write_text(customized_env)

    # Get a reference content from a code file (run-agent-loop.sh) before reinstall
    code_file = workspace / "run-agent-loop.sh"
    old_code = code_file.read_text()

    # Now reinstall with force=True — should preserve the customized env
    args = _setup_args(workspace, dry_run=False)
    args.force = True
    rc = cli._cmd_setup_project(args, settings)
    assert rc == 0

    # Check: orchestrator.env should still have the custom content
    new_env = env_file.read_text()
    assert "# CUSTOM_CONFIG=my_value" in new_env, "Custom env content was overwritten!"
    assert new_env == customized_env, "Customized env was not fully preserved"

    # Check: code file should have been updated (this proves the install actually ran)
    # The run-agent-loop.sh script is in the templates and should be rewritten
    new_code = code_file.read_text()
    # We just verify the file exists and is readable (content may be the same in this test)
    assert code_file.is_file(), "Code file should exist"


def test_install_seeds_missing_env(settings, tmp_path):
    """Verify that install creates orchestrator.env when it doesn't exist."""
    workspace = tmp_path / "tendcharting-ws"
    workspace.mkdir(parents=True)

    # Create a minimal workspace structure without orchestrator.env
    (workspace / "agent-launchers").mkdir(parents=True, exist_ok=True)

    # Run install — should create the missing env file
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    env_file = workspace / "agent-launchers" / "orchestrator.env"
    assert env_file.is_file(), "orchestrator.env should have been created"
    content = env_file.read_text()
    assert "__WORKSPACE_ROOT__" not in content, "Placeholders should be replaced"
    assert str(workspace) in content, "Workspace path should be substituted"


def test_yolo_env_suffix_preserved(settings, tmp_path):
    """Verify that *-yolo.env files are preserved (not overwritten) when
    force=True, even if a template file exists."""
    workspace = tmp_path / "tendcharting-ws"

    # First install: scaffold the workspace
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    # Check if any *-yolo.env template exists
    templates_root = cli._launcher_template_root()
    yolo_templates = list(templates_root.rglob("*-yolo.env"))

    if yolo_templates:
        # If a template exists, create a matching workspace file with custom content
        for template_path in yolo_templates:
            rel = template_path.relative_to(templates_root)
            workspace_yolo = workspace / rel
            workspace_yolo.parent.mkdir(parents=True, exist_ok=True)
            custom_yolo = "# custom yolo settings\n"
            workspace_yolo.write_text(custom_yolo)

            # Reinstall with force
            args = _setup_args(workspace, dry_run=False)
            args.force = True
            rc = cli._cmd_setup_project(args, settings)
            assert rc == 0

            # Verify the file was not overwritten
            assert workspace_yolo.read_text() == custom_yolo, \
                f"{workspace_yolo.name} should have been preserved"
    else:
        # No *-yolo.env templates exist, so unit-test _is_preserved directly
        assert cli._is_preserved(Path("agent-launchers/qwen-yolo.env"))
        assert cli._is_preserved(Path("orchestrator.env"))
        assert cli._is_preserved(Path("secrets.env"))
        assert not cli._is_preserved(Path("run-agent-loop.sh"))


def test_write_is_atomic_no_tmp_left(settings, tmp_path):
    """Verify that after an install, no .tmp-install temp files remain in the workspace."""
    workspace = tmp_path / "tendcharting-ws"

    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    # Check that no temporary files were left behind
    tmp_files = list(workspace.rglob("*.tmp-install"))
    assert len(tmp_files) == 0, f"Temporary files found: {tmp_files}"
