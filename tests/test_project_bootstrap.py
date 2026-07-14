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
    qwen_worker = workspace / "start-qwen-code-worker.sh"
    qwen_orch = workspace / "start-qwen-orch-manager.sh"
    qwen_runtime = workspace / "agent-launchers" / "runtimes" / "qwen-code.sh"
    opencode_worker = workspace / "start-opencode-worker.sh"
    opencode_orch = workspace / "start-opencode-orch-manager.sh"
    opencode_runtime = workspace / "agent-launchers" / "runtimes" / "opencode.sh"
    assert launcher.is_file()
    assert startup.is_file()
    assert qwen_worker.is_file()
    assert qwen_orch.is_file()
    assert qwen_runtime.is_file()
    assert opencode_worker.is_file()
    assert opencode_orch.is_file()
    assert opencode_runtime.is_file()
    assert "__WORKSPACE_ROOT__" not in launcher.read_text()
    assert str(workspace) in launcher.read_text()
    assert "tendcharting" in startup.read_text()
    assert not any(part.name == "__pycache__" for part in workspace.rglob("*"))
    assert os.access(workspace / "start-agent.sh", os.X_OK)
    assert os.access(qwen_worker, os.X_OK)
    assert os.access(qwen_orch, os.X_OK)
    assert os.access(qwen_runtime, os.X_OK)
    assert os.access(opencode_worker, os.X_OK)
    assert os.access(opencode_orch, os.X_OK)
    assert os.access(opencode_runtime, os.X_OK)


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
    assert "run one backend cycle for tendcharting" in " ".join(interactive_args)
    assert default_config == interactive_config
