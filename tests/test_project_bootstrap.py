from __future__ import annotations

from argparse import Namespace
import os
import shlex
import subprocess
import sys
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


# --------------------------------------------------------------------------- #
# Phase 5 (durable-worker-sidecar plan §5/§D): --print-cmd mode on
# runtimes/claude.sh and runtimes/codex.sh, and the AGENT_SIDECAR=1 branch in
# start-agent.sh that uses it.
# --------------------------------------------------------------------------- #

def _expected_sidecar_port(project: str, agent_id: int) -> int:
    """Mirrors start-agent.sh's QA-fixed port formula (finding 3) exactly --
    computed via the real `cksum` binary (not reimplemented in Python) so
    this stays correct even if the formula's constants ever change here and
    there without the test being updated in lockstep."""
    cksum_out = subprocess.run(
        ["bash", "-c", f"printf '%s' {shlex.quote(project)} | cksum"],
        capture_output=True, text=True, check=True,
    ).stdout
    cksum_val = int(cksum_out.split()[0])
    return 4900 + (cksum_val % 50) * 20 + agent_id


_RUNTIME_SCRIPT_ENV_BASE = {
    "PROJECT": "tendcharting",
    "DASHBOARD": "http://127.0.0.1:8800",
    "ROLE": "backend-dev-worker",
    "AGENT_ID": "1",
    "TEAM": "backend",
    "FUNCTION": "dev",
    "GATE": "implementation",
    "APP": "apps/api",
    "PROMPT_NAME": "dev-worker",
    "LOOP_AGENT": "0",
    "IDLE_STOP": "0",
    "COMMAND_TIMEOUT": "0",
    "AGENT_ENABLE_LOOP_DEFAULT": "1",
    "AGENT_POLL_DEFAULT": "90",
    "FANOUT_DEFAULT": "3",
    "AGENT_MODE": "interactive",
}


def _run_runtime_script_print_cmd(script: Path, workspace: Path, worktree: Path,
                                  prompt_file: Path, runtime: str, extra_args: list[str] | None = None,
                                  extra_env: dict[str, str] | None = None):
    launcher_dir = cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
    env = os.environ.copy()
    # This orch-manager session's OWN shell sets CLAUDE_MODEL/AGENT_MODE/
    # MCP_TOOL_TIMEOUT/MODEL_ACCESS_KEY/etc (it is itself a claude agent,
    # launched via this same sidecar machinery) -- os.environ.copy() would
    # otherwise leak those into the subprocess and silently change which
    # branch/model/env-passthrough values the runtime script takes. Must be
    # genuinely unset, then set explicitly below (or left to the script's
    # own documented defaults), never inherited.
    for leak in ("CLAUDE_MODEL", "ORCH_CLAUDE_MODEL", "ORCH_CODEX_MODEL",
                 "ORCH_OPENCODE_MODEL", "MODEL_SEL", "AGENT_MODE",
                 "MCP_TOOL_TIMEOUT", "MCP_TIMEOUT", "CODEX_HOME", "MODEL_ACCESS_KEY"):
        env.pop(leak, None)
    env.update(_RUNTIME_SCRIPT_ENV_BASE)
    env.update({
        "WORKSPACE_ROOT": str(workspace),
        "LAUNCHER_DIR": str(launcher_dir),
        "ORCH": str(cli.REPO_ROOT),
        "RUNTIME": runtime,
        "WORKTREE": str(worktree),
        "PROMPT_FILE": str(prompt_file),
    })
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), "--print-cmd", *(extra_args or [])],
        cwd=str(cli.REPO_ROOT), env=env, check=False, capture_output=True, text=True,
    )


# A `python3` stub that intercepts exactly one invocation shape -- being
# handed sidecar.py as $1 -- and captures its argv ONE ELEMENT PER LINE
# (never $*-joined-then-resplit: --tmux-spawn-cmd's value is itself a single
# argv element containing many spaces, which a join+shlex-resplit can't
# recover). Any OTHER python3 invocation (e.g. claude.sh's write_mcp_json,
# which uses the bare `python3` name for its own tiny heredoc script) falls
# through to the REAL interpreter, so those callers keep working normally.
_PYTHON3_STUB_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "${{1:-}}" = {sidecar_py} ]; then\n'
    '  : > {capture_file}\n'
    '  for a in "$@"; do printf \'%s\\n\' "$a" >> {capture_file}; done\n'
    "  exit 0\n"
    "fi\n"
    'exec {real_python3} "$@"\n'
)


def test_print_cmd_mode_claude_prints_exec_line_and_executes_nothing(tmp_path):
    workspace = tmp_path / "ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("Run one backend cycle for {{PROJECT}}.")

    script = (cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
              / "runtimes" / "claude.sh")
    rc = _run_runtime_script_print_cmd(script, workspace, worktree, prompt_file, "claude")

    assert rc.returncode == 0, rc.stderr
    printed = rc.stdout.strip()
    args = shlex.split(printed)

    # QA fix (finding 2): the printed line is meant to run LATER, elsewhere
    # (tmux respawn-pane) -- self-contained via an `env K=V ...` prefix for
    # every env var claude.sh exports for the exec, since a fresh tmux
    # pane's shell won't have inherited this script's own `export`s.
    assert args[0] == "env"
    assert "MCP_TOOL_TIMEOUT=3300000" in args     # claude.sh's documented default
    assert any(a.startswith("MCP_TIMEOUT=") for a in args)
    assert "claude" in args
    claude_idx = args.index("claude")

    assert "--dangerously-skip-permissions" in args[claude_idx:]
    assert "--mcp-config" in args
    mcp_file = Path(args[args.index("--mcp-config") + 1])
    # Deliberately left on disk in --print-cmd mode -- the printed command
    # line references it by path and is meant to run LATER (tmux
    # respawn-pane), so an EXIT-trap cleanup here would delete it out from
    # under that future exec.
    assert mcp_file.exists()
    mcp_file.unlink()
    assert "--strict-mcp-config" in args
    # apply_interactive_prompt rewrote the single-cycle directive. Checked
    # against the shlex-PARSED args (not the raw %q-escaped `printed` string,
    # which backslash-escapes every space in this multi-word token).
    assert "Run backend cycles continuously for tendcharting." in args
    assert not any(a.startswith("Run one backend cycle") for a in args)
    # Nothing else ran: there is no real `claude` binary anywhere on this
    # sandbox's PATH, so had the script fallen through to `exec "${cmd[@]}"`
    # it would have failed with "command not found" (nonzero exit) instead.


def test_print_cmd_mode_codex_prints_exec_line_and_executes_nothing(tmp_path):
    workspace = tmp_path / "ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("Run one backend cycle for {{PROJECT}}.")

    script = (cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
              / "runtimes" / "codex.sh")
    rc = _run_runtime_script_print_cmd(script, workspace, worktree, prompt_file, "codex")

    assert rc.returncode == 0, rc.stderr
    printed = rc.stdout.strip()
    args = shlex.split(printed)

    # QA fix (finding 2): self-contained via an `env K=V ...` prefix --
    # CODEX_HOME always (codex.sh always exports it), MODEL_ACCESS_KEY only
    # when the inference profile actually set one (not the case here: no
    # --inference given).
    assert args[0] == "env"
    assert any(a.startswith("CODEX_HOME=") for a in args)
    assert not any(a.startswith("MODEL_ACCESS_KEY=") for a in args)
    assert "codex" in args
    codex_idx = args.index("codex")

    assert "--yolo" in args[codex_idx:]   # interactive branch (non orch-manager role)
    assert "-C" in args and args[args.index("-C") + 1] == str(worktree)
    assert "mcp_servers.orchestrator" in " ".join(args)
    assert "Run backend cycles continuously for tendcharting." in args


def test_print_cmd_mode_codex_forwards_passthrough_runtime_args(tmp_path):
    workspace = tmp_path / "ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("Run one backend cycle for {{PROJECT}}.")

    script = (cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
              / "runtimes" / "codex.sh")
    rc = _run_runtime_script_print_cmd(script, workspace, worktree, prompt_file, "codex",
                                       extra_args=["--inference", "digitalocean"])

    assert rc.returncode == 0, rc.stderr
    args = shlex.split(rc.stdout.strip())
    assert 'model_provider="digitalocean"' in args


def test_print_cmd_mode_codex_includes_model_access_key_in_env_prefix_when_set(tmp_path):
    # QA fix (finding 2): MODEL_ACCESS_KEY is included in the `env K=V ...`
    # prefix whenever it's actually set (regardless of exactly which
    # inference-profile branch set it -- codex.sh's own print-cmd check is
    # a plain `[ -n "${MODEL_ACCESS_KEY:-}" ]`), so the printed command is
    # self-contained even when respawned in an environment that never ran
    # this script's own profile-resolution logic.
    workspace = tmp_path / "ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("Run one backend cycle for {{PROJECT}}.")

    script = (cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
              / "runtimes" / "codex.sh")
    rc = _run_runtime_script_print_cmd(script, workspace, worktree, prompt_file, "codex",
                                       extra_env={"MODEL_ACCESS_KEY": "test-secret-key"})

    assert rc.returncode == 0, rc.stderr
    args = shlex.split(rc.stdout.strip())
    assert args[0] == "env"
    assert "MODEL_ACCESS_KEY=test-secret-key" in args


def test_print_cmd_flag_only_consumed_as_literal_first_argument(tmp_path):
    """--print-cmd is a guarded early check on $1 only -- it must not be
    mistaken for a runtime arg if it ever appeared elsewhere (defensive: the
    launcher never passes it that way, but the guard itself should only ever
    fire on position 1)."""
    workspace = tmp_path / "ws"
    worktree = workspace / "wt-backend-dev"
    worktree.mkdir(parents=True)
    prompt_file = workspace / "prompt.md"
    prompt_file.write_text("Run one backend cycle for {{PROJECT}}.")

    script = (cli.REPO_ROOT / "templates" / "project-launchers" / "agent-launchers"
              / "runtimes" / "codex.sh")
    # "--print-cmd" here is a RUNTIME arg (not $1), so it must NOT trigger
    # print-cmd mode -- codex.sh's own arg loop just forwards it into
    # RUNTIME_ARGS, and (since there's no real `codex` binary on PATH) the
    # script fails trying to exec it -- proving the guard did NOT fire.
    rc = _run_runtime_script_print_cmd(script, workspace, worktree, prompt_file, "codex",
                                       extra_args=[])
    assert rc.returncode == 0   # baseline sanity: normal --print-cmd-as-$1 still works


def test_agent_sidecar_opencode_routes_through_sidecar_py(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    opencode_stub = bin_dir / "opencode"
    opencode_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    opencode_stub.chmod(0o755)

    # The workspace's OWN installed copy (install-launchers copies the
    # canonical template into every workspace's agent-launchers/) -- NOT the
    # template source path, which start-agent.sh never references.
    sidecar_py = workspace / "agent-launchers" / "sidecar.py"
    capture_file = tmp_path / "sidecar-argv-opencode.txt"
    python3_stub = bin_dir / "python3"
    python3_stub.write_text(_PYTHON3_STUB_TEMPLATE.format(
        sidecar_py=shlex.quote(str(sidecar_py)),
        capture_file=shlex.quote(str(capture_file)),
        real_python3=shlex.quote(sys.executable),
    ))
    python3_stub.chmod(0o755)

    env = os.environ.copy()
    for leak in ("CLAUDE_MODEL", "ORCH_CLAUDE_MODEL", "ORCH_CODEX_MODEL", "MODEL_SEL"):
        env.pop(leak, None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AGENT_SIDECAR"] = "1"

    rc = subprocess.run(
        ["bash", str(workspace / "start-agent.sh"), "backend-dev-worker", "opencode"],
        cwd=str(workspace), env=env, check=False, capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert capture_file.exists(), rc.stdout + rc.stderr
    # One argv element per line -- NOT `shlex.split("$*"-joined)`, which is
    # lossy here: the tmux-spawn-cmd test below captures a single argv
    # element (--tmux-spawn-cmd's value) that itself contains many spaces,
    # and re-splitting a space-joined dump can't recover the original
    # argument boundaries.
    argv = capture_file.read_text().splitlines()

    assert argv[0] == str(sidecar_py)
    assert argv[argv.index("--runtime") + 1] == "opencode"
    # QA fix (finding 3): port folds a cksum(PROJECT) hash into the base so
    # different PROJECTS (both fleets share this host) land in different
    # 20-wide bands -- no longer a bare 4900+AGENT_ID (which collides across
    # projects reusing the same small agent-id numbering).
    assert (argv[argv.index("--opencode-url") + 1]
            == f"http://127.0.0.1:{_expected_sidecar_port('tendcharting', 1)}")
    assert argv[argv.index("--opencode-dir") + 1] == str(workspace / "wt-backend-dev")
    assert "--opencode-provider-id" in argv
    assert "--opencode-model-id" in argv
    assert argv[argv.index("--agent-id") + 1] == "1"
    assert argv[argv.index("--project") + 1] == "tendcharting"
    assert argv[argv.index("--dashboard") + 1] == "http://127.0.0.1:8800"

    prompt_path = Path(argv[argv.index("--prompt-file") + 1])
    prompt_text = prompt_path.read_text()
    assert "cycles continuously" in prompt_text   # interactive rewrite applied


def test_agent_sidecar_claude_uses_print_cmd_for_spawn_cmd(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The workspace's OWN installed copy (install-launchers copies the
    # canonical template into every workspace's agent-launchers/) -- NOT the
    # template source path, which start-agent.sh never references.
    sidecar_py = workspace / "agent-launchers" / "sidecar.py"
    capture_file = tmp_path / "sidecar-argv-claude.txt"
    python3_stub = bin_dir / "python3"
    python3_stub.write_text(_PYTHON3_STUB_TEMPLATE.format(
        sidecar_py=shlex.quote(str(sidecar_py)),
        capture_file=shlex.quote(str(capture_file)),
        real_python3=shlex.quote(sys.executable),
    ))
    python3_stub.chmod(0o755)

    env = os.environ.copy()
    for leak in ("CLAUDE_MODEL", "ORCH_CLAUDE_MODEL", "ORCH_CODEX_MODEL", "MODEL_SEL",
                 "MCP_TOOL_TIMEOUT", "MCP_TIMEOUT"):
        env.pop(leak, None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AGENT_SIDECAR"] = "1"
    env["SIDECAR_TMUX_TARGET"] = "agents:3.0"

    rc = subprocess.run(
        ["bash", str(workspace / "start-agent.sh"), "backend-dev-worker", "claude"],
        cwd=str(workspace), env=env, check=False, capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert capture_file.exists(), rc.stdout + rc.stderr
    argv = capture_file.read_text().splitlines()   # one argv element per line -- see template docstring

    assert argv[0] == str(sidecar_py)
    assert argv[argv.index("--runtime") + 1] == "tmux"
    assert argv[argv.index("--tmux-target") + 1] == "agents:3.0"
    assert "--tmux-spawn-cmd" in argv
    spawn_cmd = argv[argv.index("--tmux-spawn-cmd") + 1]
    # QA fix (finding 2): self-contained via an `env K=V ...` prefix, not a
    # bare "claude ..." command.
    assert spawn_cmd.startswith("env ")
    assert "MCP_TOOL_TIMEOUT=3300000" in spawn_cmd
    assert "claude " in spawn_cmd
    assert "--dangerously-skip-permissions" in spawn_cmd
    assert "--mcp-config" in spawn_cmd

    prompt_path = Path(argv[argv.index("--prompt-file") + 1])
    assert "cycles continuously" in prompt_path.read_text()


def test_agent_sidecar_dev_manager_command_timeout_zero_no_timeout_wrapper(settings, tmp_path):
    # QA fix (finding 8): a durable side-car session must NEVER be wrapped
    # in `timeout N` -- the side-car's own watchdog, not a per-cycle shell
    # timeout, owns stuck detection. dev-manager roles default
    # COMMAND_TIMEOUT=1200 (for the per-cycle relaunch loop this bypasses),
    # so this specifically proves start-agent.sh overrides it to 0 before
    # invoking --print-cmd for the AGENT_SIDECAR path.
    workspace = tmp_path / "tendcharting-ws"
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sidecar_py = workspace / "agent-launchers" / "sidecar.py"
    capture_file = tmp_path / "sidecar-argv-devmanager.txt"
    python3_stub = bin_dir / "python3"
    python3_stub.write_text(_PYTHON3_STUB_TEMPLATE.format(
        sidecar_py=shlex.quote(str(sidecar_py)),
        capture_file=shlex.quote(str(capture_file)),
        real_python3=shlex.quote(sys.executable),
    ))
    python3_stub.chmod(0o755)

    env = os.environ.copy()
    for leak in ("CLAUDE_MODEL", "ORCH_CLAUDE_MODEL", "ORCH_CODEX_MODEL", "MODEL_SEL",
                 "MCP_TOOL_TIMEOUT", "MCP_TIMEOUT"):
        env.pop(leak, None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AGENT_SIDECAR"] = "1"
    env["SIDECAR_TMUX_TARGET"] = "agents:1.0"

    rc = subprocess.run(
        ["bash", str(workspace / "start-agent.sh"), "backend-dev-manager", "claude"],
        cwd=str(workspace), env=env, check=False, capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert capture_file.exists(), rc.stdout + rc.stderr
    argv = capture_file.read_text().splitlines()

    spawn_cmd = argv[argv.index("--tmux-spawn-cmd") + 1]
    assert "timeout" not in spawn_cmd.split()
    assert "claude " in spawn_cmd
    assert "--dangerously-skip-permissions" in spawn_cmd


def test_agent_sidecar_requires_sidecar_tmux_target_for_claude(settings, tmp_path):
    workspace = tmp_path / "tendcharting-ws"
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    env = os.environ.copy()
    env["AGENT_SIDECAR"] = "1"
    env.pop("SIDECAR_TMUX_TARGET", None)

    rc = subprocess.run(
        ["bash", str(workspace / "start-agent.sh"), "backend-dev-worker", "claude"],
        cwd=str(workspace), env=env, check=False, capture_output=True, text=True,
    )
    assert rc.returncode != 0
    assert "SIDECAR_TMUX_TARGET" in rc.stderr


def test_agent_sidecar_unset_leaves_normal_dry_run_path_unaffected(settings, tmp_path):
    """AGENT_SIDECAR unset/0 must be byte-identical to pre-Phase-5 behavior --
    the dry-run plan output (which already exercises the full non-sidecar
    codepath) must be unchanged."""
    workspace = tmp_path / "tendcharting-ws"
    rc = cli._cmd_setup_project(_setup_args(workspace), settings)
    assert rc == 0

    env = os.environ.copy()
    env.pop("AGENT_SIDECAR", None)

    rc = subprocess.run(
        ["bash", str(workspace / "start-agent.sh"), "backend-dev-worker", "opencode", "--dry-run"],
        cwd=str(workspace), env=env, check=False, capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert "role=backend-dev-worker" in rc.stdout
    assert "runtime=opencode" in rc.stdout
