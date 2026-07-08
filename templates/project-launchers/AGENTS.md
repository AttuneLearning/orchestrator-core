# __PROJECT_NAME__ Agent Startup

For orch-manager sessions, read and follow `ORCH_MANAGER_STARTUP.md` in this
directory. It is the shared startup source for Codex, Qwen Code, and Claude.

When launched through `./start-orch-manager.sh codex`, use the `orchestrator`
MCP tools as the coordinator source of truth and operate as:

```text
role=orch-manager
team=orchestration
function=lead
project=__PROJECT_NAME__
```
