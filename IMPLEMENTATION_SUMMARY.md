# Implementation Summary — Autonomous Multi-Agent Orchestrator

> Context document for future Claude Code sessions. Dense and factual; trust it
> over memory, verify against code when exact signatures matter.
> State as of 2026-06-12: **feature-complete vs. docs/ORCHESTRATION_ROADMAP.md
> + ADR governance. 117 tests passed, 1 skipped (pgvector ordering — needs the
> pgvector Docker image).**

## 1. What this is

A plain-Python (no LangChain) autonomous multi-agent development orchestrator.
Canonical state lives in **Postgres**; agents act **only** through a repository
layer / MCP tools; humans observe and intervene via a FastAPI dashboard and CLI.
It is the Postgres-backed, autonomous evolution of the file-based
`dev_communication/` protocol from the upstream `agent-workflow` repo
(public: github.com/AttuneLearning/agent-workflow @ 555ff00; ported, not
vendored here) — pipeline #1, the
roster, skills, and comms rules are ported faithfully from that spec
(`PROCESS_GUIDE.md`, `protocol.yaml`, `registry.yaml`, `profiles.json`).

```
goal → decompose (reasoner) → issues per team → assign idle agents
     → per-gate: work step (code agent / cli session) + gate review (reasoner)
     → focus/off-rails sweep → re-engagement → goal reconcile
messages (cross-team) → triage → local issue → … → comms_response replies
ADR rules (Postgres) → injected per-issue into plan + review prompts
```

## 2. Invariants — do not break these

1. **All writes go through `orchestrator/repository.py`.** Engine, MCP tools,
   dashboard, CLI — nobody else touches SQL. This keeps the append-only
   `issue_events` log complete; off-rails detection, audit, and the dashboard
   timeline depend on it.
2. **`pipelines.py`, `state_machine.py`, `adr_rules.py`, `engine/focus.py` are
   pure** (no I/O). New gates/transitions/rules = table/config changes + pure tests.
3. **The engine never edits or executes repos.** Gates run in one of two modes
   (`mode:` in pipelines.yaml). **verdict** (default): the reasoner/human renders
   a decision in-process. **pull**: a registered `external` worker edits/tests in
   its OWN repo and reports pointers (`code_committed`/`tests_run`) via MCP; the
   engine assigns + observes but runs no worker. The legacy push code-gen +
   apply/verify leg (flag-off, disposable worktrees, human-only `apply-promote`,
   never pushes) survives for the autonomous/stub mode only. See `docs/PULL_AGENTS.md`.
4. **State transitions validate via `state_machine.validate_transition`.**
   `off_rails → in_progress` requires `directive=True` (only
   `repository.apply_directive` sets it).
5. **Stub providers keep everything runnable hermetically** (no API key, no
   network). Never write a test that needs a real model.
6. **Reasoner capabilities are optional/duck-typed**: `assess_complexity`,
   `triage_message`, `suggest_adr` via `getattr`; `rules=` params via
   try/except TypeError (`Engine._call_with_rules`). Old reasoners must keep working.
7. **Messages cross team boundaries; issues stay local** (protocol.yaml). An
   inbound message only ever creates an issue owned by the receiving team.
   Responses (`kind='response'`) are never re-ingested (prevents ping-pong).

## 3. Data model (Postgres, migrations/0001–0007)

| Table | Purpose / notable columns |
|---|---|
| goals | title, state (backlog/planning/active/paused/done), pipeline |
| issues | goal_id, parent_id+depth (sub-issues), team, pipeline, state, gate_type, retry_count, step_count, assigned_agent, triggered_by_message, origin_message_id, work_type |
| issue_events | append-only log: per-issue monotonic seq, event_type, from/to_state, JSONB payload |
| agents | team, function (dev/qa), runtime (api/cli), status (idle/busy/offline), last_seen |
| memory_notes | scope (global / pod:* / agent:N), body, embedding_v vector(256) when pgvector present |
| messages | from/to_team, subject, body, priority, kind (request/response), status (pending/triaged/rejected/archived/sent), issue_id |
| adrs | adr_key (ADR-{DOMAIN}-{NNN}), status (proposed/accepted/superseded/deprecated), decision (compact directive agents see), context (rationale, humans only), applies_to JSONB {work_types,teams,repos}, related/supersedes/patterns TEXT[], proposed_by |

Event types (`models.EventType`): created, state_change, gate_enter, gate_pass,
gate_decline, code_generated, error, drift_score, reengaged, context_snapshot,
plan, directive, comms_response, decomposed, verification, promoted, adr_proposed.

## 4. Issue state machine & pipelines

```
backlog → (plan + work_type tag + optional decompose) → ready → in_progress
        → in_review[gate] → (pass: next gate | decline: retry++; cap → failed) → done
blocked: decomposed parent waits on children (all done → ready; any failed/off_rails → failed)
off_rails: latched quarantine (signal ∧ drift < threshold); exit ONLY via human directive
```

Pipelines (`config/pipelines.yaml`): **pipeline-1** intake→implementation→qa_gate→
completion→comms_response (last gate conditional on `triggered_by_message`; gate
owners dev/dev/qa/dev/dev); **hotfix** implementation→qa_gate; **research**
intake→completion. Goals carry `pipeline`; decomposed issues inherit it.

## 5. Engine tick (`engine/loop.py`, single-threaded, MVCC)

Per `tick()`: load accepted ADR rules → `_ingest` (messages→triage→local issues)
→ `_decompose` (goals→issues) → `_unblock` (resolve decomposed parents) →
`_assign` (plan w/ rules + work_type tag + architect `_maybe_decompose` + claim)
→ `_advance` (work step: implementation via api/cli worker, qa_gate apply+verify
when enabled, comms_response reply; then review step: gate_review w/ rules,
violated_rules → payload; on DONE w/ no rules → `_maybe_suggest_adr`) → `_sweep`
(mechanical signals after last directive ∧ drift → quarantine) → `_reengage`
(step budget exhausted → snapshot + reset, once) → `_reconcile` (goals done/paused).
`run(max_ticks, on_tick)` stops when quiescent; `run_daemon(interval)` forever.

## 6. Agents & providers

- **Reasoner** (`agents/reasoning.py`): `AnthropicReasoner` (structured JSON;
  ops: decompose_goal, plan_issue, gate_review, score_drift, triage_message,
  assess_complexity, suggest_adr) when `ANTHROPIC_API_KEY` set, else
  deterministic `StubReasoner` (passes gates, drift 1.0, never decomposes/proposes).
- **Code worker (push/stub)**: `ApiWorker` → `providers.make_code_client`
  (`CODE_PROVIDER` stub | openai-compatible | anthropic). `CliSessionWorker` for
  runtime=cli. Used only on **verdict** pipelines / autonomous mode; the engine
  runs it in-process. Worker chosen per assigned agent's runtime.
- **Pull workers (`runtime=external`)**: live Claude Code / Codex / Aider, one per
  repo, registered as agents. On a **pull** gate the engine claims an idle external
  worker of the gate's `owner` function (per-gate (re)assignment in `_assign`;
  dev→qa handoff across implementation→verification) and is hands-off in `_advance`.
  The worker polls via MCP (`list_my_work`/`adr_list`), edits/tests in its repo,
  and reports (`report_work`→`code_committed`) + advances (`gate_decision`). Liveness:
  `_reclaim` offlines a worker stale past `agent_stale_seconds` and unassigns;
  `reclaim_cap` reclaims → off_rails. `heartbeat` MCP tool refreshes `last_seen`.
  Reference loop: `examples/pull-agent/loop.py`; setup: `docs/PULL_AGENTS.md`.
- **Roster** (`config/roster.yaml` → `roster.py`): active teams backend(api,
  repos=[api-repo]), frontend(ui, [web-repo]), qa(quality, []), platform(plat, []).
  A team needs a **dev and a qa agent registered** or issues stall at qa_gate.

## 7. ADR governance (`adr_rules.py` + adrs table)

Rules are data; the engine knows only selectors → inject/verify. Selection =
work_type ∩ team ∩ repos (empty dim = match-all; `repos:[]` = project-wide;
team with no repo mapping receives project-wide only). Agents get
`format_rules_block` = one directive line per applicable rule, re-selected every
plan/review call. Gate reviewers verify each rule; declines cite ids
(`violated_rules` in gate_decline payload). Lifecycle: anything can **propose**
(MCP `adr_create`, or gap detection when an issue completes ungoverned, deduped
against pending proposals); only humans **approve** (`cli adr approve`,
dashboard `/adrs`) — approval marks `supersedes` targets superseded.
Work-type detection: keyword priority list in `adr_rules.WORK_TYPE_KEYWORDS`
(auth-change > new-endpoint > new-model > testing > bug-fix > general).

## 8. Surfaces

**CLI** (`python -m orchestrator.cli …`): migrate · register-agent --team
--function --runtime · add-goal "T" [--description] [--pipeline] · run
[--max-ticks | --daemon --interval] · status · directive <issue> resume [--note]
· goal-resume <goal> · propose-goal "T" [--suggested-by --source] · goal-promote
<goal> · goal-reject <goal> · adr list|show|approve [key] [--status] ·
apply-promote <issue> [--note] · serve [--transport stdio|http] (http stubbed)
· serve-dashboard [--host --port].

**Dashboard** (FastAPI, `dashboard/app.py`, `create_app(pool, settings)`):
GET / (fleet, focus %, banner, suggested goals, flagged+quarantined issues,
paused goals w/ resume), /goals/{id}, /issues/{id} (full timeline), /agents
(stale heartbeat flag), /adrs + /adrs/{key} (backlinks incl. computed reverse),
/api/state JSON; POST /issues/{id}/directive, /goals/{id}/resume,
/goals/{id}/promote, /goals/{id}/reject, /adrs/{key}/approve. Read rollups
(`fleet_summary`, `agents_with_staleness`) live in `monitoring.py`, shared with
the MCP status tools so dashboard and agents never disagree.

**MCP** (24 tools, `mcp_server/`): issues list/get/claim/update_state/
gate_decision/create_subissue/append_log/apply_directive · memory write/recall/
search (embeds when configured) · adr_create/adr_list/adr_get/adr_approve ·
comms_send/comms_check · context_load · reflect · refine · status tools for
external looping agents get_status/get_alerts/tail_events/propose_goal (the
plugin surface — see docs/PLUGIN_INTEGRATION.md).

## 9. Config (env > config/*.yaml; see .env.example)

DATABASE_URL · ANTHROPIC_API_KEY, REASONING_MODEL · CODE_PROVIDER/BASE_URL/
MODEL/API_KEY · EMBED_PROVIDER(stub|openai|none)/BASE_URL/MODEL/API_KEY ·
CLI_AGENT_CMD · APPLY_ENABLED/APPLY_REPO_PATH/VERIFY_CMD · thresholds:
DRIFT_THRESHOLD(0.5) RETRY_CAP(3) STEP_BUDGET(25) MAX_DEPTH(3) MAX_SUBISSUES(8)
MAX_ISSUES_PER_GOAL(30). Ops UIs: `docker compose --profile ops up -d`
(Directus :8055, Metabase :3000, read-only role orchestrator_ro).

## 10. Testing & verification

- `pytest -q` → 117 passed, 1 skipped. DB-backed tests use conftest `pool`
  fixture (auto-migrate + per-test TRUNCATE). Pure tests (pipelines,
  state_machine, focus/offrails, adr_rules pure parts) need no DB.
- **One Postgres = one test runner.** Concurrent suite runs truncate each other
  mid-flight and produce phantom failures (FK violations, wrong counts).
- Hermetic end-to-end: no keys → stubs; `register-agent` (dev+qa) → `add-goal`
  → `run --max-ticks 50` → `status` shows everything done, deterministic.
- Custom test reasoners subclass StubReasoner or implement the protocol subset;
  thresholds via `copy.deepcopy(settings)`.

## 11. File map

```
migrations/0001..0007         schema (init, messages_triage, pgvector, readonly_role,
                              goal_pipeline, agent_heartbeat, adr_governance,
                              goal_suggestions) — next: 0009
config/{settings,pipelines,roster}.yaml
orchestrator/
  config.py db.py models.py repository.py        # settings, pool+migrate, dataclasses, ALL SQL
  state_machine.py pipelines.py roster.py adr_rules.py embeddings.py   # pure logic
  agents/{base,reasoning,providers,api_worker,cli_session}.py
  engine/{loop,focus,offrails,reengagement}.py
  mcp_server/{server,tools_issues,tools_memory,tools_skills}.py
  dashboard/{app,templates}.py                   # templates = plain-Python HTML, no Jinja
  apply/worktree.py                              # apply/verify leg (flag-off)
  cli.py
tests/ (12 files)  docs/{ORCHESTRATION_ROADMAP,INSTALL_LMS,IMPLEMENTATION_SUMMARY}.md
```

Spec source (ported, **not** vendored): the upstream `agent-workflow` repo
(github.com/AttuneLearning/agent-workflow @ 555ff00) — skills, PROCESS_GUIDE,
scaffolds. Clone separately if you need the originals.

## 12. Known limitations / honest notes

- Implementation artifacts are **single stored blobs** (`generated/issue-{id}.txt`
  in worktrees), not surgical multi-file edits. Mode B = draft + sandboxed
  verify + gated capture, not an autonomous contributor.
- ADR enforcement is **reviewer-checked** (model verifies, declines citing ids)
  — structural enforcement primitives (e.g. auto-inserted gates) were considered
  and deliberately deferred.
- `context_load` MCP tool pulls memory only (not ADRs); ADRs reach agents via
  engine prompt injection instead.
- One orchestrator DB = one project. Multi-project = separate DBs.
- Dashboard has no auth — bind localhost only.
- History quirk: built in a cloud sandbox whose commit-signing was broken and
  repo had no remote; work was delivered as a 6-patch chain (core, A+B, C, D,
  E–J, adr-governance) applied/committed locally by the user.

## 13. Extending (follow these patterns)

- New SQL → new numbered migration; never edit applied ones. New writes → a
  repository.py function appending proper events.
- New reasoner op → add to Protocol + StubReasoner (deterministic) +
  AnthropicReasoner (extract_json), call via optional-capability pattern.
- New gate/pipeline → config/pipelines.yaml + pure tests; gates carry owner
  (dev|qa), optional condition + on_failure.
- New dashboard page → repository read fn + templates fn + route + TestClient test.
- Deferred candidates: structural ADR enforcement, multi-file artifact apply,
  pgvector-backed ADR retrieval at >100 rules, dashboard auth, agent-team hooks
  from the agent-workflow spec.
