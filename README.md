# Autonomous Multi-Agent Orchestrator (python-orchestrator-v1)

A plain-Python orchestrator that drives software work goals through an
autonomous multi-agent pipeline. Canonical state lives in **Postgres**; agents
act only through an **MCP tool layer**; a single-threaded **engine loop**
advances each issue through the five-phase pipeline defined by the
[`agent-workflow`](./agent-workflow) spec (included as a submodule).

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
cp .env.example .env   # set DATABASE_URL; ANTHROPIC_API_KEY optional (see below)

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

- **Reasoning agent** (`agents/reasoning.py`) uses the Anthropic SDK
  (`api.anthropic.com`, reachable from the cloud env) when `ANTHROPIC_API_KEY`
  is set. Without a key it falls back to a deterministic stub so the pipeline
  runs and the test suite passes hermetically.
- **Code agent** (`agents/providers.py`) is configurable via `CODE_PROVIDER`:
  - `stub` (default) — deterministic placeholder output, no network.
  - `openai` — any OpenAI-compatible endpoint (`CODE_BASE_URL` / `CODE_MODEL` /
    `CODE_API_KEY`).
  - `anthropic` — reuse the Anthropic SDK for code generation.

> The Qwen box `10.100.90.132:8081` is a **private LAN IP, unreachable from the
> Anthropic cloud env**. To use it, point `CODE_PROVIDER=openai` at a publicly
> reachable endpoint (added to the environment's Custom allowed domains) or run
> via a teleported local session.

## MCP server

```bash
.venv/bin/python -m orchestrator.cli serve   # FastMCP over stdio
```

Tools: issue (`list_issues`, `get_issue`, `claim_issue`, `update_state`,
`gate_decision`, `create_subissue`, `append_log`), memory (`memory_write`,
`memory_recall`, `memory_search`), and skill tools ported from `agent-workflow`
(`adr_create`, `comms_send`, `context_load`, `reflect`, `refine`).

## Tests

```bash
.venv/bin/python -m pytest -q
```

Pure-function suites (`test_pipelines`, `test_state_machine`,
`test_focus_offrails`) need no database; `test_repository` runs against the
configured `DATABASE_URL`.

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
agent-workflow/    spec source (submodule, read-only)
```
