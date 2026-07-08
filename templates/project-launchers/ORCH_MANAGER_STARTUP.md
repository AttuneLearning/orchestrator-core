# __PROJECT_NAME__ Orch-Manager Startup

This is the shared startup source for Qwen Code, Codex, and Claude when a CLI
session is acting as the `__PROJECT_NAME__` orchestrator manager.

## Identity

- Project: `__PROJECT_NAME__`
- Role: `orch-manager`
- Team: `orchestration`
- Function: `lead`
- Runtime: current CLI runtime (`qwen`, `codex`, or `claude`)
- Worktree: workspace root, not a product worktree
- Product implementation: do not implement product code unless explicitly asked

The orch-manager is a supervisory console over the coordinator. Normal pull
workers register as numbered agents and heartbeat with `agent_id`; the
orch-manager role is intentionally not a normal pull worker and usually has no
`agent_id`.

## Required MCP Connection

Use the orchestrator MCP server as the source of truth. It must point at this
project coordinator:

```text
command: __ORCH_PATH__/.venv/bin/python
args: -m orchestrator.cli --instance __PROJECT_NAME__ serve
PYTHONPATH: __ORCH_PATH__
ORCH_INSTANCE: __PROJECT_NAME__
```

The launcher scripts in this workspace inject that MCP configuration for Codex
and Claude. For Qwen Code, the launcher also ensures a project-scoped MCP server
named `orchestrator` exists via `qwen mcp add --scope project`.

## Register The Current CLI Session As Orch-Manager

To start a fresh orch-manager session from the workspace root:

```bash
./start-orch-manager.sh qwen
./start-orch-manager.sh codex
./start-orch-manager.sh claude
```

To verify the launch configuration without starting a session:

```bash
./start-orch-manager.sh --dry-run qwen
./start-orch-manager.sh --dry-run codex
./start-orch-manager.sh --dry-run claude
```

Expected dry-run identity:

```text
role=orch-manager
team=orchestration
function=lead
project=__PROJECT_NAME__
agent_id=
worktree=__WORKSPACE_ROOT__
prompt=__WORKSPACE_ROOT__/agent-launchers/prompts/orch-manager.md
```

Inside an already-running compatible CLI session, treat yourself as registered
for orch-manager work when all of these are true:

1. You are in `__WORKSPACE_ROOT__`.
2. The `orchestrator` MCP tools are available.
3. The MCP server is connected to the `__PROJECT_NAME__` coordinator.
4. You are operating under this startup file and the orch-manager prompt.

If MCP tools are missing, restart through `./start-orch-manager.sh <runtime>`.
If the MCP server points at another project, restart with the launcher or fix the
runtime MCP config before taking coordinator actions.

## First Tick Checklist

At the start of each management tick:

1. Check coordinator status.
2. Check pending messages.
3. Check active goals and blocked or stale issues.
4. Check agent health, loops, and recent heartbeats.
5. Decide whether to create/update goals, issues, ADRs, contracts, messages, or
   dashboard state.

Keep decisions visible through orchestrator records: ADRs, issue comments,
messages, gate decisions, or dashboard state.

## Operating Rules

- Use orchestrator MCP tools before local files for coordination state.
- Prefer small, explicit management actions over broad intervention.
- Do not directly edit product code as orch-manager unless the user explicitly
  asks for implementation.
- Do not merge, promote, or push as a runtime CLI; the orchestrator owns local
  promotion.
- When routing work, preserve team/function boundaries and assign issues to the
  correct registered pull worker.
- When escalating, include the issue, target agent or team, reason, and the next
  concrete action expected.
- If a worker is stuck, inspect status and messages before reassigning work.

## Useful Local Commands

```bash
# Coordinator status from the orchestrator repo
cd __ORCH_PATH__
.venv/bin/python -m orchestrator.cli --instance __PROJECT_NAME__ status

# Start the daemon if it is not running
ORCH_INSTANCE=__PROJECT_NAME__ \
setsid .venv/bin/python -m orchestrator.cli --instance __PROJECT_NAME__ run --daemon --interval 5 \
  >/tmp/__PROJECT_NAME__-daemon.log 2>&1 < /dev/null &
```

Dashboard:

```text
__DASHBOARD_URL__
```

Settings:

```text
__DASHBOARD_URL__/settings?project=__PROJECT_NAME__
```

Orch-manager Codex launches read dashboard-managed `orch_manager_codex` model
profile settings by default. Explicit runtime flags such as
`--inference digitalocean` still override dashboard selection. Prefer
`model_profiles.<name>.api_key_env` for secrets so config stores an environment
variable name rather than the API key itself.
