# Using the orchestrator as a plugin for external looping agents

This orchestrator exposes its full surface to external **looping agents**
(Hermes, OpenClaw, Open Interpreter, and any MCP-capable runtime) through its
**MCP server**. A looping agent can watch fleet progress the way a human watches
the dashboard, raise alerts when something needs attention, and suggest new goals
for the orchestrator to work on — without ever touching Postgres or the engine
internals.

The orchestrator stays in control of execution: external agents **observe and
suggest**; the engine and a human operator decide what actually runs.

## Connecting (stdio)

The MCP server runs over **stdio** today. Any MCP client launches it as a
subprocess:

```bash
python -m orchestrator.cli serve         # stdio (default)
```

Register it with your agent using the sample config in
[`examples/mcp-client-config.json`](../examples/mcp-client-config.json) — set the
`command` to your venv Python, `cwd` to the project root, and `DATABASE_URL` to
your Postgres. No auth is required for stdio (it's a local subprocess); bind
nothing to the network.

> **Remote / HTTP — coming soon.** `serve --transport http` is scaffolded but not
> yet implemented: it parses `--host`/`--port` and reads `ORCH_MCP_TOKEN` for a
> future bearer-token check, then exits with a notice. Use stdio until the HTTP/SSE
> transport lands so co-located hosting isn't required.

## The three integration capabilities

### 1. Monitor progress

| Tool | Returns |
|---|---|
| `get_status()` | Full fleet rollup: goal/issue counts by state, `fleet_focus` %, the attention set, the agent registry, and pending suggestions. Same data the dashboard renders at `/`. |
| `tail_events(after_id=0, limit=200)` | Cross-issue event feed, oldest-first. Each row carries a global `id` (your cursor), `issue_id`, `event_type`, state transition, and `payload`. |
| `list_issues` / `get_issue` | Existing per-issue detail. |

**Polling loop:** start with `tail_events(after_id=0)`, remember the largest `id`
you receive, and pass it as `after_id` next time to get only new events. That is
your progress stream.

### 2. Signal alerts (watch the "dashboard")

`get_alerts()` returns just the attention set — what needs action right now:

```jsonc
{
  "below_threshold": true,             // single "is anything wrong" boolean
  "fleet_focus": 0.6,
  "flagged_issues": [ {"id": 12, "state": "in_progress",
                       "signals": ["oscillation"], "title": "..."} ],
  "paused_goals":   [ {"id": 3, "state": "paused", "title": "..."} ],
  "stale_agents":   [ {"id": 5, "team": "backend", "stale": true} ]
}
```

Call it each loop; when `below_threshold` is `true`, surface the contents as an
alert. `flagged_issues` includes both drifting issues (mechanical signals:
`retry_cap`, `step_budget`, `repeated_errors`, `oscillation`) and quarantined
`off_rails` issues. This is the exact predicate the engine uses to quarantine, so
your alerts never disagree with the engine.

### 3. Suggest next steps / goals (gated)

`propose_goal(title, description="", pipeline="pipeline-1", suggested_by="agent",
source="")` records a goal in the **`suggested`** state. Suggested goals are
**inert** — the engine never decomposes or assigns them — until a human promotes
them. This is the human-in-the-loop gate (the same propose → approve pattern as
ADRs).

A human reviews suggestions on the dashboard home page ("Suggested goals" — with
**Promote** / **Reject** buttons) or via the CLI:

```bash
python -m orchestrator.cli goal-promote <goal_id>   # suggested → backlog (runs)
python -m orchestrator.cli goal-reject  <goal_id>   # suggested → rejected
```

Agents can also propose governance rules with `adr_create` (also gated by
`adr_approve`) and send cross-team notes with `comms_send`.

## Recommended agent loop

```
loop:
  events = tail_events(after_id=cursor)         # progress since last poll
  cursor = max(e.id for e in events) or cursor
  alerts = get_alerts()
  if alerts.below_threshold:
      notify(alerts)                            # raise the alert
  if <you have an idea for new work>:
      propose_goal(title=..., suggested_by="my-agent", source="why")
  sleep(interval)
```

A human (or another policy) then promotes the proposals worth running, and the
orchestrator's engine takes it from there.
