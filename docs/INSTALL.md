# Install & Adopt the Orchestrator for Your Own Project

This is the authoritative getting-started guide. It takes you from a fresh clone
to **your own project registered and workers running**. If you only want a
throwaway smoke test, the [README Quickstart](../README.md#quickstart) is the
5-command version; this guide covers real adoption.

The orchestrator is a **shared, multi-instance coordinator**: one install serves
N projects. A project is an *instance* — a `(database + roster + settings)`
triple declared in `config/instances.yaml` and selected with `--instance <key>`
(or the `ORCH_INSTANCE` env var). You do **not** fork the repo per project; you
add an instance block.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **PostgreSQL 16 or 17** | Reachable on a TCP port. `pgvector` is optional (semantic memory degrades gracefully to `ILIKE` without it). |
| **Python 3.11+** | Developed and tested on 3.12. |
| `git` | Workers do their coding in git worktrees of your product repo. |
| **Node + your test runner** | Only if you use the verify/QA gates — the harness runs *your* `verify_cmd` (e.g. `npm run typecheck && npm test`). |
| A model endpoint (optional) | Any OpenAI-compatible or Anthropic endpoint for the reasoner/workers. Without one, the engine runs on a deterministic **stub** so you can smoke-test the pipeline offline. |

> **This is run-from-source, not a pip package.** There is no `setup.py` /
> `pyproject.toml`. You run it as a module (`python -m orchestrator.cli …`) from
> the repo root, which puts the package on `PYTHONPATH`.

---

## 2. Install the core

```bash
git clone <this-repo> orchestrator-core
cd orchestrator-core

python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Configuration model (read this once)

Config is resolved from three layers, **process env wins over both YAML files**:

1. `config/settings.yaml` — global defaults (the `Settings` dataclass).
2. `config/instances.yaml` — per-instance overrides, deep-merged onto the globals
   when you pass `--instance <key>`.
3. Process environment — every documented env var overrides the resolved value.

> **`.env` is NOT auto-loaded.** Despite older wording in some docs, the app does
> not read a `.env` file for its own config (`orchestrator/config.py` says so
> explicitly). Real config = the two YAML files + exported environment variables.
> [`.env.example`](../.env.example) at the repo root lists the env vars you may
> want to export (`source .env` yourself, or set them in your shell/service
> manager) — it is a **reference to export**, not something the app ingests.

---

## 3. Create your project's database

Each instance uses its own database. Create it and the login role once:

```bash
# local Postgres
sudo -u postgres psql -c "CREATE ROLE orchestrator LOGIN PASSWORD 'orchestrator' SUPERUSER;"
sudo -u postgres createdb myproject -O orchestrator
```

or with the bundled container (Postgres + pgvector):

```bash
docker compose up -d db          # creates the 'orchestrator' database; add others with createdb
docker compose exec db createdb -U orchestrator myproject
```

The migration runner does **not** create the role or database — it assumes they
exist. (Migrations are otherwise self-contained and idempotent, and grant a
read-only `orchestrator_ro` role against *whatever* database you migrate, so any
DB name works.)

---

## 4. Register your project as an instance

Add a block to `config/instances.yaml`. A fully-annotated template lives in
[`config/instances.example.yaml`](../config/instances.example.yaml) — copy the
`myproject` block from there. Minimal shape:

```yaml
instances:
  myproject:
    label: My Project
    database_url: postgresql://orchestrator:orchestrator@localhost:5432/myproject
    roster_file: config/roster.myproject.yaml     # created in the next step
    settings:                                      # per-project overrides
      promote_repo_path: /abs/path/to/your/product-checkout
      promote_branch: main
      docs_path: /abs/path/to/your/docs
      verify_cmd: npm run typecheck && npm test
      verify_worktrees:
        backend: /abs/path/to/workspace/wt-backend-qa
        frontend: /abs/path/to/workspace/wt-frontend-qa
      engine_reasoner:                             # see §7 for model config
        profile: digitalocean
        model: <your-reasoner-model>
```

> **Override the global promote/docs paths.** The shipped `config/settings.yaml`
> defaults `promote_repo_path`/`docs_path` to the maintainer's own checkout. If
> you don't override them per-instance, auto-promote would target the wrong repo.
> Always set them in your instance's `settings:` block.

### Create your roster

The roster declares your teams, their functions (dev/qa/lead), how each is
staffed (`mode: pull` = a live external coding agent owns the work;
`mode: verdict` = the engine/reviewer decides), and the `repos:` labels used to
scope ADR rules. Copy the annotated pull template:

```bash
cp config/roster.example.pull.yaml config/roster.myproject.yaml
$EDITOR config/roster.myproject.yaml       # set team ids, issue_prefix, repos, modes
```

Key points from the template header:
- `repos:` entries are **routing/ADR-scoping labels, not filesystem paths**.
- A `mode: pull` gate needs a *registered external worker* of that function, or
  its issues park unworked until one appears.
- The instance's `roster_file` wins over any ambient `ROSTER_FILE` env var.

---

## 5. Migrate and register agents

```bash
# apply schema (21 migrations, idempotent) to YOUR instance's database
.venv/bin/python -m orchestrator.cli --instance myproject migrate

# register at least a dev AND a qa agent per team you defined in the roster
.venv/bin/python -m orchestrator.cli --instance myproject register-agent --team backend --function dev
.venv/bin/python -m orchestrator.cli --instance myproject register-agent --team backend --function qa
```

Agents get sequential numeric IDs. For the **pull** model (live coding agents),
register them with `--runtime external`.

---

## 6. Scaffold the workspace & launchers

Workers run *outside* the core: each is a coding-agent CLI (Claude Code, Codex,
OpenCode, Qwen Code) looping in a git worktree of your product repo, talking to
the core over MCP. The `setup-project` command stamps the launcher kit
(`templates/project-launchers/`) into your workspace and creates the per-worker
worktree directories:

```bash
.venv/bin/python -m orchestrator.cli setup-project \
  --workspace /abs/path/to/your/workspace \
  --project   myproject \
  --dashboard-url http://127.0.0.1:8800
```

This copies `start-*.sh`, `agent-launchers/`, and the templated
`CLAUDE.md`/`AGENTS.md`/`ORCH_MANAGER_STARTUP.md`, substituting four tokens:
`__WORKSPACE_ROOT__`, `__ORCH_PATH__`, `__PROJECT_NAME__`, `__DASHBOARD_URL__`.
(`install-launchers` does the copy without creating worktrees.)

> **Known adoption caveat — edit `agent-launchers/roles.sh` by hand.** The
> scaffolded `roles.sh` maps each role to a fixed agent ID, worktree name, and
> package path (`apps/api`, `apps/web`, `packages/contracts`, `wt-backend-dev`,
> …), modeled on a specific monorepo. The scaffolder does **not** derive these
> from your roster, so if your team names, repo layout, or worktree naming
> differ, edit `roles.sh` after scaffolding to match. This is the single biggest
> hand-edit an adopter needs.

Agent docs (`CLAUDE.md`/`AGENTS.md`/`QWEN.md`) inside each worktree are
**generated** from the core (accepted ADRs ∩ the team's repos) and re-rendered on
every worker launch — never edit them by hand. You can regenerate manually:

```bash
.venv/bin/python -m orchestrator.cli --instance myproject render-agent-docs \
  --team backend --function dev --agent-id 1 --out-dir /path/to/wt-backend-dev
```

---

## 7. Point the reasoner and workers at your models

There are **two independent model systems**:

**(a) Engine reasoner** — the in-process brain (decompose goals, gate reviews,
drift scoring). Selected by `settings.reasoner` (`stub | anthropic | openai |
cli`) or, more conveniently, by an `engine_reasoner.profile` in your instance.
Define profiles under `model_profiles` in `config/settings.yaml`:

```yaml
model_profiles:
  digitalocean:
    base_url: https://inference.do-ai.run/v1
    model: <model>
    api_key_env: MODEL_ACCESS_KEY     # the ENV VAR NAME holding the key, not the key
```

Setting `engine_reasoner.profile` auto-maps it onto `reasoner=openai` +
base_url/model/key unless you've set the `REASONER_*` env vars explicitly.
With no profile and no `ANTHROPIC_API_KEY`, the reasoner falls back to the
deterministic **stub** (fine for offline smoke tests).

**(b) Worker/CLI models** — the external coding agents. Driven by the scaffolded
`agent-model.yaml` (the source of truth for the launcher `-m <shortcut>` switch)
plus provider base-URLs in `agent-launchers/lib.sh` and secrets in
`agent-launchers/secrets.env` (copy from `secrets.env.example`; it holds e.g.
`MODEL_ACCESS_KEY`). Point these at your own endpoints.

> The shipped model endpoints reference the maintainer's private LAN hosts
> (`10.100.90.132:*`) and a DigitalOcean subscription. **These are examples** —
> repoint `model_profiles`, `agent-model.yaml`, and `lib.sh` at endpoints you can
> actually reach.

---

## 8. Run it

```bash
# add a goal (route through the pull pipeline for live coding agents)
.venv/bin/python -m orchestrator.cli --instance myproject add-goal \
  "Add a health-check endpoint to the API" --pipeline pull-1

# drive the engine: bounded (until quiescent) …
.venv/bin/python -m orchestrator.cli --instance myproject run --max-ticks 50
# … or as a long-running daemon
.venv/bin/python -m orchestrator.cli --instance myproject run --daemon --interval 5

# inspect
.venv/bin/python -m orchestrator.cli --instance myproject status
```

Then launch workers from your workspace (they self-pace via the `heartbeat` MCP
tool and re-render their agent docs on start):

```bash
cd /abs/path/to/your/workspace
./start-dev-worker.sh backend opencode
./start-qa-worker.sh   backend
```

### Long-running services

| Service | Command | Port |
|---|---|---|
| Engine daemon | `run --daemon --interval 5` | — |
| Dashboard (FastAPI) | `serve-dashboard --host 0.0.0.0 --port 8800` | **8800** in practice (the CLI default is 8000; the maintainer's launcher uses 8800 — pick one and be consistent) |
| MCP server | `serve` | stdio only (`--transport http` is stubbed) |

The dashboard is multi-project: switch coordinators with `?project=<key>`.

---

## 9. Verify a healthy install

```bash
.venv/bin/python -m pytest -q          # requires a reachable DATABASE_URL
```

All tests should pass. (Older docs quote specific counts — 30/92/117/141 — which
have all drifted; trust "all green," not a number.) For a clean-machine *engine*
smoke test with no external endpoints, force the stub reasoner:

```bash
REASONER=stub .venv/bin/python -m orchestrator.cli --instance myproject run --max-ticks 20
```

---

## 10. Adoption caveats & known hardcodes

These don't block a config-driven adoption but are worth knowing:

1. **`agent-launchers/roles.sh`** is monorepo-shaped with fixed agent IDs and
   worktree/package paths — hand-edit after scaffolding (see §6).
2. **Dashboard source links** are built against a specific GitHub repo
   (`orchestrator/dashboard/templates.py`) and are not yet configurable — file
   links will point at the wrong repo for other adopters.
3. **`ACCEPTANCE_DIR`** in the dashboard defaults to a maintainer path; override
   it with the `ACCEPTANCE_DIR` env var.
4. **Global `promote_repo_path`/`docs_path`** default to the maintainer's
   checkout — always override per-instance (§4).
5. **Migration `0004`** creates an `orchestrator_ro` analytics role with a
   default password — change it before any internet-facing deployment.

---

## See also

- [`PULL_AGENTS.md`](PULL_AGENTS.md) — the live-coding-agent (pull) integration in depth.
- [`PLUGIN_INTEGRATION.md`](PLUGIN_INTEGRATION.md) — connect an external monitoring/suggesting agent over MCP.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — components, tick order, gate ownership, guardrails.
- [`../README.md`](../README.md) — overview, quickstart, command reference, ops UIs (Directus/Metabase).
