# Autonomous Multi-Agent Orchestrator (python-orchestrator-v1)

A plain-Python orchestrator that drives software work goals through an
autonomous multi-agent pipeline. Canonical state lives in **Postgres**; agents
act only through an **MCP tool layer**; a single-threaded **engine loop**
advances each issue through the five-phase pipeline defined by the upstream
[`agent-workflow`](https://github.com/AttuneLearning/agent-workflow) spec
(ported from @ 555ff00; not vendored in this repo).

This is the **phases 1–4 orchestration core**. Deferred to follow-ups: the
FastAPI ops dashboard, Directus admin, pgvector semantic search, the `cli`
(`--resume`) agent runtime, Metabase, and additional pipelines/teams.

## Architecture

```
cli.py ──► engine/loop.py (tick: ingest → decompose → assign → advance gate
   │                              → focus/off-rails sweep → re-engage → reconcile)
   │              │
   │              ├─► agents/reasoning.py   Anthropic SDK (stub fallback w/o key):
   │              │                          decompose_goal · plan_issue ·
   │              │                          gate_review · score_drift
   │              └─► agents/api_worker.py  code generation via providers.py
   │                                         (stub | openai | anthropic)
   ▼
mcp_server/* (issue · memory · skill tools) ──┐
                                              ├─► repository.py (all SQL) ─► Postgres
engine/loop.py ───────────────────────────────┘
```

Both the engine and the MCP tools mutate state **only** through `repository.py`,
so every change is recorded in the append-only `issue_events` log that
off-rails / oscillation detection depends on. Generated code is **stored, never
executed**.

### Pipeline #1 — the five-phase lifecycle

Seeded faithfully from `agent-workflow` (`PROCESS_GUIDE.md` / `protocol.yaml`)
in `config/pipelines.yaml`:

`intake → implementation → qa_gate → completion → comms_response`

`comms_response` is conditional — skipped unless the issue was triggered by an
inbound cross-team message. A `qa_gate` decline routes back to `implementation`.

### Issue state machine

```
backlog → planning → ready → in_progress → in_review[gate] → done
                                 ↑________________| (decline → retry_count++)
   blocked            failed (retry cap)   off_rails (quarantine, latched)
```

Off-rails latches only when a **mechanical signal** (retry cap, step budget,
repeated errors, decline oscillation) fires **and** the Code Drift Reviewer's
`score_drift` is below `DRIFT_THRESHOLD`.

## Quickstart (in-session Postgres)

The cloud env ships Postgres 16; no Docker required.

```bash
service postgresql start
sudo -u postgres psql -c "CREATE ROLE orchestrator LOGIN PASSWORD 'orchestrator' SUPERUSER;"
sudo -u postgres createdb orchestrator -O orchestrator

python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m orchestrator.cli migrate
.venv/bin/python -m orchestrator.cli register-agent --team backend --function dev
.venv/bin/python -m orchestrator.cli register-agent --team backend --function qa
.venv/bin/python -m orchestrator.cli add-goal "Add a health-check endpoint to the API"
.venv/bin/python -m orchestrator.cli run --max-ticks 50
.venv/bin/python -m orchestrator.cli status
```

`docker compose up -d db` is an optional alternative (pgvector image) if you
prefer a containerized database.

## Agents & providers

- **Reasoning agent** (`agents/reasoning.py`) is configured in
  `config/settings.yaml`. Anthropic mode uses `ANTHROPIC_API_KEY` from the
  process environment; without a key it falls back to a deterministic stub so
  the pipeline runs and the test suite passes hermetically.
- **Code agent** (`agents/providers.py`) is configurable via `CODE_PROVIDER`:
  - `stub` (default) — deterministic placeholder output, no network.
  - `openai` — any OpenAI-compatible endpoint (`CODE_BASE_URL` / `CODE_MODEL` /
    `CODE_API_KEY`).
  - `anthropic` — reuse the Anthropic SDK for code generation.

> The Qwen box `10.100.90.132:8081` is a **private LAN IP, unreachable from the
> Anthropic cloud env**. To use it, point `CODE_PROVIDER=openai` at a publicly
> reachable endpoint (added to the environment's Custom allowed domains) or run
> via a teleported local session.

### Dashboard-managed model profiles

The dashboard settings page can manage shared OpenAI-compatible model profiles:

```text
http://10.100.55.87:8800/settings?project=tendcharting
```

Profiles live under `model_profiles` and are consumed by role settings such as
`orch_manager_codex`, `orch_manager_claude`, `engine_reasoner`, and
`devqa_worker`. Project-specific values are stored under
`config/instances.yaml` at `instances.<project>.settings`; global defaults live
in `config/settings.yaml`.

The default Codex orch-manager profile is DigitalOcean:

```yaml
model_profiles:
  digitalocean:
    base_url: https://inference.do-ai.run/v1
    model: deepseek-v4-pro
    wire_api: chat
    api_key_env: MODEL_ACCESS_KEY
orch_manager_codex:
  profile: digitalocean
  reasoning_effort: high
```

Use `api_key_env` to avoid storing secrets in plaintext. The dashboard stores
only the environment variable name, and launcher resolution retrieves the actual
secret from that process environment. Direct `api_key` values remain supported
for local compatibility but should be treated as plaintext config.

When `engine_reasoner.profile` is set, the loader maps the selected profile onto
the existing OpenAI-compatible reasoner fields (`reasoner=openai`,
`reasoner_base_url`, `reasoner_model`, and `reasoner_api_key`) unless the
corresponding legacy environment variables are set.

DigitalOcean tool-calling uses the chat-completions request shape. `tool_choice`
is a top-level sibling of `tools`:

```json
{
  "model": "deepseek-v4-pro",
  "messages": [
    { "role": "user", "content": "What is the weather in Austin?" }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
          "type": "object",
          "properties": {
            "city": { "type": "string" }
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

## MCP server

```bash
.venv/bin/python -m orchestrator.cli serve   # FastMCP over stdio
```

Tools: issue (`list_issues`, `get_issue`, `claim_issue`, `update_state`,
`gate_decision`, `create_subissue`, `append_log`), memory (`memory_write`,
`memory_recall`, `memory_search`), skill tools ported from `agent-workflow`
(`adr_create`, `comms_send`, `context_load`, `reflect`, `refine`), and status
tools for external monitors (`get_status`, `get_alerts`, `tail_events`,
`propose_goal`).

### Plugin for external looping agents

The MCP server doubles as a plugin surface for looping agents (Hermes, OpenClaw,
Open Interpreter, …): they **monitor** progress (`get_status` / `tail_events`),
**alert** on the attention set (`get_alerts`), and **suggest** goals
(`propose_goal`) that land gated in a `suggested` state until a human promotes
them (`goal-promote`). See [`docs/PLUGIN_INTEGRATION.md`](docs/PLUGIN_INTEGRATION.md)
and the sample [`examples/mcp-client-config.json`](examples/mcp-client-config.json).
(`serve --transport http` is scaffolded but stubbed; stdio works today.)

## Tests

```bash
.venv/bin/python -m pytest -q
```

Pure-function suites (`test_pipelines`, `test_state_machine`,
`test_focus_offrails`) need no database; `test_repository` runs against the
configured `DATABASE_URL`.

## Contract Acceptance Error Handling

Fixed 500 Internal Server Error when accepting contracts in the dashboard.
Previously, attempting to accept a contract with no pending proposal would raise
a ValueError that wasn't caught, causing a 500 error. Now, such errors are
caught and return a user-friendly 400 error page instead.

## Layout

```
config/            settings.yaml · pipelines.yaml · roster.yaml
migrations/        ordered .sql, applied by orchestrator/db.py
orchestrator/
  config.py db.py models.py repository.py
  state_machine.py pipelines.py roster.py
  agents/   base.py providers.py reasoning.py api_worker.py
  engine/   loop.py focus.py offrails.py reengagement.py
  mcp_server/ server.py tools_issues.py tools_memory.py tools_skills.py
  cli.py main.py
tests/
```

> Spec source: the upstream `agent-workflow` repo
> (github.com/AttuneLearning/agent-workflow @ 555ff00). It was ported into the
> files above and is **not** vendored here — clone it separately if you need the
> original PROCESS_GUIDE/protocol/registry or the skill-pack installer (§ ops).

## Ops UIs (Directus + Metabase)

Directus (admin/inspect) and Metabase (analytics) run under the `ops` compose
profile so they do not start by default.

### Starting the ops stack

```bash
# Postgres only (default — unchanged):
docker compose up -d db

# Postgres + Directus + Metabase:
docker compose --profile ops up -d
```

### URLs

| Service   | URL                    | Default credentials                          |
|-----------|------------------------|----------------------------------------------|
| Directus  | http://localhost:8055  | `DIRECTUS_ADMIN_EMAIL` / `DIRECTUS_ADMIN_PASSWORD` from the process environment (defaults: `admin@example.com` / `Admin1234!`) |
| Metabase  | http://localhost:3000  | Set up on first visit via the setup wizard   |

### First-time setup

**Metabase app database** — Metabase stores its own metadata in a separate
`metabase` Postgres database. Create it once before starting the stack (or after
the `db` container is running):

```bash
docker compose exec db createdb -U orchestrator metabase
```

**Directus first login** — On first startup Directus seeds the admin account
using `DIRECTUS_ADMIN_EMAIL` and `DIRECTUS_ADMIN_PASSWORD` from your process environment.
Change these from the defaults before any internet-facing deployment.

### Connecting Metabase to the orchestrator data

Use the read-only role created by migration `0004_readonly_role.sql` so
Metabase cannot mutate live data:

1. Open http://localhost:3000 and complete the setup wizard.
2. Go to **Settings → Databases → Add a database**.
3. Choose **PostgreSQL** and fill in:
   - Host: `db` (or `localhost` if Metabase is running outside Docker)
   - Port: `5432`
   - Database name: `orchestrator`
   - Username: `orchestrator_ro`
   - Password: `orchestrator_ro`
4. Save — Metabase will sync the schema and you can build questions/dashboards.

The `orchestrator_ro` role has `SELECT` on all existing and future tables in the
`public` schema (via `ALTER DEFAULT PRIVILEGES`).

### Read-only role (direct psql access)

```bash
PGPASSWORD=orchestrator_ro psql -h localhost -U orchestrator_ro -d orchestrator
```

The role can `SELECT` from all tables but any `INSERT`/`UPDATE`/`DELETE` will
be rejected with a permission error.

### WARNING — Directus write bypass

> **Edits made through the Directus admin UI bypass the `issue_events` audit
> log.** The orchestrator's off-rails and oscillation-detection logic depends on
> every state change being recorded in `issue_events` via `repository.py`.
> Prefer the CLI dashboard directives (`orchestrator.cli`) for all normal state
> changes. Use Directus only for **read/inspect** and as an **emergency escape
> hatch** (e.g. manually unsticking an `off_rails` issue when the engine cannot
> self-recover).

## Command reference (post-roadmap slices)

| Command | Slice | What it does |
| --- | --- | --- |
| `run --daemon --interval 5` | A | tick forever; per-tick summaries; Ctrl-C stops |
| `directive <issue-id> resume --note "..."` | B | un-quarantine an `off_rails` issue (audited) |
| `goal-resume <goal-id>` | B | restart a `paused` goal |
| `serve-dashboard --port 8000` | C | FastAPI ops dashboard (fleet, timelines, directives) |
| `add-goal "..." --pipeline hotfix` | J | route a goal through an alternate pipeline |
| `apply-promote <issue-id> --note "..."` | F | merge an issue's *verified* worktree branch (human gate; local only) |
| `install-launchers --workspace /path/to/ws --project <name>` | Ops | install parent-dir Claude/Codex/Qwen launcher scripts for a project workspace |

Cross-team messages ingest automatically each tick (slice D): a pending request
to a rostered team is triaged into a local issue; its completion sends a
response and archives the original. Sub-issue decomposition (slice E) splits
oversized issues and blocks the parent until children finish. `runtime=cli`
agents (slice I) run `CLI_AGENT_CMD` per implementation step. Semantic memory
(slice H) embeds notes when `EMBED_PROVIDER` is set and degrades to ILIKE
without pgvector. The apply/verify leg (slice F) is **off by default**
(`APPLY_ENABLED`): artifacts apply only in disposable worktrees, and nothing
merges without `apply-promote`.

## ADR governance

Architecture decisions are live rules that govern agent work, not documents.
Each rule = a compact one-line directive (`decision`) + selector
(`applies_to: {work_types, teams, repos}`; empty dimension = match-all,
`repos: []` = project-wide) + rationale (`context`, humans only) + backlink
edges (`related`, `supersedes`, `patterns`).

Every plan and gate review receives exactly the rules matching the issue's
coordinates (work-type ∩ team ∩ the team's repos from `config/roster.yaml`) —
the most concise applicable list by construction, re-selected on every call.
Gate reviewers verify each rule and cite violated ids in the decline payload
(`violated_rules`), so chronic violations are visible in `issue_events`.

Lifecycle: agents (or sessions, via the `adr_create` MCP tool) **propose**;
proposals are inert until a human **approves** (`cli adr approve`, or the
dashboard `/adrs` page) — approval also marks superseded rules. When an issue
completes with no governing rules, gap detection drafts a proposal for review.

```bash
.venv/bin/python -m orchestrator.cli adr list --status proposed
.venv/bin/python -m orchestrator.cli adr show ADR-API-001
.venv/bin/python -m orchestrator.cli adr approve ADR-API-001
```
