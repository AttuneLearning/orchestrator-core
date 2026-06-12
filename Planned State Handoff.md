# Plan State / Handoff: Autonomous Multi-Agent Orchestrator (python-orchestrator-v1)

> **What this document is.** A planning *handoff*, not a finished implementation plan. It captures
> everything established in the first planning session, corrects the draft plan's wrong assumptions
> about the repo, records the environment facts that change the design, and tells the *next* session
> exactly what to read and resolve to produce the faithful, executable plan.
>
> **Why it exists.** This session was started from the wrong repo — the one cloned here is the old
> LangChain "AI Workflow System", **not** the repo that contains the `agent-workflow/` spec. The real
> spec (skills, `dev_communication/`, `PROCESS_GUIDE.md`, roster) is in a different repo that isn't in
> this container. Plan: download this doc → start a new Claude Code session inside the correct repo
> (with `agent-workflow/` present) → run a cloud ultraplan there against the real files.

---

## 1. Reality vs. the draft plan (corrections)

The draft plan assumed a layout that does **not** exist in the container this session was cloned into
(`/home/user/repo/`). Confirmed by filesystem search:

| Draft assumed | Actually in this container |
| --- | --- |
| `python-orchestrator-v1/` exists | **Does not exist** |
| `langchain-qwen-project/` is a sibling subdir | **Does not exist** — the LangChain project **is the repo root** (`/home/user/repo/src/`) |
| `agent-workflow/` present as a "spec source" to port from | **Not in this container at all** (it lives in another repo) |
| Skill packs `*/skills/*/SKILL.md`, `PROCESS_GUIDE.md`, issue template, roster present | **None present here** |

**What is actually in this repo** (`/home/user/repo/`, branch `master`, no git remote configured): a
LangChain skeleton — `src/core/{memory_system,skill_executor,workflow_manager}.py`,
`src/agents/{claude_agents,claude_code_agent,codex_agents}.py`, `src/workflows/{context,reflection,refinement}_workflow.py`.
Mostly stubs/thin wrappers. Uses invalid model ids (`claude-3-opus-20240229`, `claude-3-codex-20240229`).
No DB, no MCP, no Docker, **no real Qwen integration** despite the name. This is the project the plan
calls "retire."

**Consequence:** the skill set, pipeline #1, and roster are **not yet readable** by me. The next
session must plan against the real `agent-workflow/` files, not reconstruct them.

---

## 2. What I *was* able to capture (from user-pasted material)

Enough to anchor the design, not enough to finalize it.

**`agent-workflow/` package shape** (from README + `agent-coord-setup.sh`):
- `claude-workflow/` — Claude command/skill assets + `setup.sh`
- `codex-workflow/` — Codex skill pack + `install.sh` (supports `--detect-team`, `--list-teams`, per-team install)
- `agent-coord-setup.sh` — unified entrypoint (`--both` | `--claude-only` | `--codex-only`, team auto-detect)

**Skills** = `/comms /adr /memory /context /reflect /refine`. Captured in full: `adr`, `memory`, `context`.
**Still needed:** `comms`, `reflect`, `refine`, and the whole `codex-workflow` pack.

- **`/adr`** (`adr.md`): actions `status|check|gaps|suggest|poll|create|review` over
  `dev_communication/shared/architecture/{index.md,decision-log.md,decisions/,templates/,suggestions/,gaps/}`.
  CREATE reads `templates/adr-template.md`, writes `decisions/ADR-{DOMAIN}-{NNN}-{TITLE}.md`, updates decision-log + index.
- **`/memory`** (`memory.md`): `note|add|search|entity|pattern|session|status` over a memory vault. Note = append
  `- **YYYY-MM-DD**: <text>` to `memory/notes.md`. Entity/pattern/session use templates + update `memory-log.md`.
  Uses Obsidian wikilinks `[[entities/x]]`. **Note the path drift:** `/memory` references `memory/`, while
  `/context` references `ai_team_config/memory_store/` — the setup script migrates `memory/` → `ai_team_config/memory_store/`.
  Resolve canonical memory root in next session.
- **`/context`** (`context.md`): pre-implementation loader. Quick mode reads `ai_team_config/memory_store/memory-log.md`
  + `context/project-overview.md`. Full mode detects work-type from keywords, loads ≤3 ADRs + ≤4 patterns + memory,
  emits a context block. Token budget <2000.

**Templates captured** (frontmatter + sections):
- `adr-template.md`: `id, title, status, date, domain, patterns` → Decision / Context / Consequences(+/−) / Related.
- `pattern-template.md`: `name, parent_adr, status, work_types, created, usage_count` → When / Template / Variations / Checklist.
- `session-template.md`: `date, title, issues, patterns_used, patterns_created` → Summary / Changes / Patterns / Decisions / Follow-up.

**`dev_communication/` protocol** (from `setup.sh`): per-team workspaces (`backend/`, `frontend/`, …) each with
`inbox/` + `issues/{queue,active,completed}/`, plus `shared/architecture/{decisions,suggestions,gaps,templates}`,
`shared/{guidance,specs,plans,contracts}`, `templates/`, `archive/`, governed by `PROCESS_GUIDE.md` and
`shared/registry.yaml`. Teams have a `definition.yaml` + `status.md`. Memory vault at `ai_team_config/memory_store/`
(`entities/`, `patterns/`, `sessions/`, `context/`, `team-configs/`, `memory-log.md`). Agent-team mode adds
`.claude/hooks/{task-completed,teammate-idle}.sh`.

**Still needed before faithful planning:** `PROCESS_GUIDE.md` (the real pipeline stages/gates/lifecycle — the single
most important file), `shared/registry.yaml` + a team `definition.yaml` (the roster/pods/roles), issue + message
templates, and the `codex-workflow` skill pack.

---

## 3. Environment facts (correct the draft's verification assumptions)

Claude Code on the web cloud sessions (per official docs):
- **Postgres 16 and Docker + compose are pre-installed**, just not running. Start with `service postgresql start`
  (or `docker compose up`). So the DB layer and most end-to-end verification **can run in-session** — the draft's
  pessimism about "can't run the stack here" was wrong.
- **`api.anthropic.com` is allowlisted** (Trusted) → the Anthropic reasoning agent works in-cloud.
- **The Qwen box `10.100.90.132:8081` is a private LAN IP → unreachable from Anthropic's cloud**, regardless of
  allowlist. The OpenAI-compatible code-agent leg must use a publicly reachable endpoint (add `api.openai.com` or a
  public host under **Custom** allowed domains) or run via a teleported local session.
- **Second repo into the container** = clone it in the environment **Setup script** (cached, runs as root pre-launch).
  `github.com` is already allowlisted.

**Setup-script recipe** (environment settings → Setup script):
```bash
#!/bin/bash
set -e
if [ ! -d /home/user/agent-workflow ]; then
  git clone --depth 1 \
    "https://x-access-token:${GH_TOKEN}@github.com/<OWNER>/agent-workflow.git" \
    /home/user/agent-workflow
fi
```
Add env var `GH_TOKEN=<PAT>` if the repo is private (the built-in GitHub proxy only auths the *primary* repo). If
public, drop the `x-access-token:${GH_TOKEN}@`. **Simpler alternative:** vendor `agent-workflow/` into the main repo
(or add as a submodule + `git submodule update --init` in the setup script) — then it's always present, no token.

---

## 4. Locked decisions (carried from the draft, still valid)

Postgres (Docker Compose / or in-session `service postgresql start`) canonical · MCP as the single agent interface ·
skills ported to MCP tools (slash-commands become thin wrappers over the same tools) · scoped flat memory + pgvector
(no Obsidian graph, no edge graph) · configurable YAML pipelines/roster (pods) with the existing flow as pipeline #1 ·
agent runtime `api`|`cli` per registry, **API-worker first** · focus = drift score + mechanical signals · retire the
LangChain project.

---

## 5. Target architecture (refined, provisional)

Plain-Python orchestrator (no LangChain). Canonical state in Postgres. Agents act only through an MCP tool layer.
Humans observe via Directus (admin) + a custom ops dashboard; Metabase later.

```
docker-compose.yml          # pgvector/pgvector:pg17 (or in-session PG16); directus + metabase later
.env.example                # DATABASE_URL, ANTHROPIC_API_KEY, CODE_PROVIDER/BASE_URL/MODEL, thresholds
requirements.txt            # anthropic, openai, fastapi, uvicorn, psycopg[binary,pool], mcp, pyyaml, python-dotenv
migrations/                 # ordered .sql, run by db.py (reproducible without Docker)
config/  pipelines.yaml  roster.yaml  settings.yaml
orchestrator/
  config.py db.py models.py repository.py          # SQL, dataclasses, enums
  state_machine.py pipelines.py roster.py          # lifecycle + gate resolution + registry
  agents/ base.py providers.py api_worker.py cli_session.py reasoning.py
  engine/ loop.py reengagement.py focus.py offrails.py
  mcp_server/ server.py tools_issues.py tools_memory.py tools_skills.py
  dashboard/ app.py templates.py
  cli.py main.py
tests/
```

**Data model (Postgres):** `goals`, `issues`, `issue_events` (append-only processing log — source for
oscillation/audit/timeline), `agents` (registry), `memory_notes` (scope `global|pod:*|agent:*`, pgvector phase 2),
`messages`, `adrs`. (Field lists in the draft are a good starting point; reconcile against real `PROCESS_GUIDE.md`
+ templates before locking.)

**State machine (core, gates from config):**
```
backlog → planning → ready → in_progress → in_review[gate_type] → done
                                  ↑______________| (decline: retry_count++)
  + blocked (sub-issues/deps)   + failed (retry cap)   + off_rails (quarantine)
```
`in_review` carries a `gate_type`; each issue's pipeline (from `pipelines.yaml`) is an ordered gate list.
**Pipeline #1** provisionally `plan → implement → code_review → qa → done` — **must be confirmed against the real
`PROCESS_GUIDE.md`.** `pipelines.py` pure/table-driven → unit-testable. Sub-issues via Architect decomposition
(`parent_id`, inherited `goal_id`, `depth+1`), caps `MAX_DEPTH/MAX_SUBISSUES/MAX_ISSUES_PER_GOAL`.

**Agents/runtime:** reasoning via Anthropic SDK (default `claude-opus-4-8`, structured JSON: `decompose_goal`,
`plan_issue`, `gate_review`, `score_drift`); code via `openai` SDK behind configurable `base_url`. Generated code
stored, **never auto-executed**. Runtime `api` (build first) | `cli` (phase 2, `--resume`). Re-engagement
(`reengagement.py`): detect exhaustion → persist context to Postgres → re-seed fresh window from issue +
`memory_notes` + recent `issue_events` → resume.

**Engine loop (single-threaded tick, Postgres MVCC):** ingest goals → decompose → assign → advance one step →
focus/off-rails sweep → re-engage exhausted → reconcile goals. Each issue wrapped in try/except.

**Focus & off-rails:** mechanical signals (retry cap, step budget, repeated errors, state oscillation from
`issue_events`) gate the Code Drift Reviewer `score_drift`; off-rails latched only when a signal fires **and**
drift `< DRIFT_THRESHOLD`. Fleet `%` raises dashboard banner. Goal → `paused` on focus failure; dashboard supplies
a directive to restart.

**MCP layer:** issue tools (`list/get/claim/update_state/gate_decision/create_subissue/append_log`), memory tools
(`memory_write/recall/search`), skill tools ported from the real SKILL.md set (`adr_*`, `comms_*`, `context_load`,
`reflect`, `refine`). Agents never write except through MCP (preserves the audit log off-rails depends on).

---

## 6. Open questions to resolve in the next session

1. **Pipeline #1 ground truth** — read `PROCESS_GUIDE.md`; map its real stages/gates/roles to the state machine.
   Does the file-based `queue/active/completed` + per-team `inbox` map cleanly to `issues.state` + `messages`?
2. **Roster** — read `shared/registry.yaml` + team `definition.yaml`; derive pods/roles/default-enabled set. Confirm
   the draft's pod catalog (Product/Engineering/Quality/Operations/Adversarial) vs. what actually exists.
3. **Full skill set** — read `comms`, `reflect`, `refine`, and the `codex-workflow` pack; finalize the MCP tool
   surface and the slash-command→tool wrappers.
4. **Canonical memory root** — `memory/` vs `ai_team_config/memory_store/`; how the flat-scope `memory_notes` table
   relates to the file vault (mirror? replace? export?).
5. **Where the orchestrator lives** relative to `agent-workflow/` (sibling dir, subdir, or submodule) and what
   "retire the LangChain project" concretely means in the real repo.
6. **Migration scope for this PR** — phases 1–4 (infra+schema → MCP → agents → engine) as the first reviewable slice,
   with dashboard/Directus/CLI-runtime/pgvector/Metabase/retire as follow-ups.

---

## 7. Recommended next-session procedure

1. Confirm `agent-workflow/` is present (setup script worked). If not, fix the clone/token first.
2. **Read first, in order:** `agent-workflow/.../dev_communication/PROCESS_GUIDE.md`, `shared/registry.yaml`,
   one team `definition.yaml`, `claude-workflow/skills/{comms,reflect,refine}.md`, the `codex-workflow` pack,
   `dev_communication/templates/*` (issue + message), and the ADR/pattern/session templates.
3. Resolve §6 open questions (use AskUserQuestion where genuinely ambiguous).
4. Produce the faithful executable plan (which files, what order, how to verify), then ultraplan it.
5. **Verification can run in-session:** `service postgresql start` → `cli migrate` → `register-agent` →
   `add-goal` → watch issues flow through pipeline #1; exercise off-rails, re-engagement, focus-halt; run
   pure-function + repository unit tests. Code-agent leg needs a publicly reachable OpenAI-compatible endpoint
   (the Qwen LAN box won't work in-cloud).

---

## 8. Provisional build phases (unchanged from draft, re-validate after reading the real spec)

1. Infra + schema: compose, migrations, `config.py`, `db.py`, `models.py`, `repository.py` (+ repository tests).
2. MCP server + issue/memory tools.
3. Agents: `providers.py`, `api_worker.py`, `reasoning.py` (structured outputs).
4. Engine: `state_machine.py` + `pipelines.py` + `loop.py` + `focus.py` + `offrails.py` + `reengagement.py` (+ pure-fn tests).
5. Ops dashboard (FastAPI) + Directus wiring + `cli.py` (`add-goal`, `run`, `serve`, `register-agent`).
6. Phase 2: CLI runtime (`--resume`), pgvector + `memory_search`, Metabase, extra pipelines/pods, alert channels.
7. Retire the LangChain project (archive + README pointer).
