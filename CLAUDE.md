# Orchestrator — agent context

Plain-Python autonomous multi-agent dev orchestrator. Postgres = canonical
state; FastAPI dashboard + CLI for humans; MCP tools for agents. Ported from
the upstream `agent-workflow` spec (github.com/AttuneLearning/agent-workflow
@ 555ff00; PROCESS_GUIDE.md 5-phase pipeline).

**Read `docs/IMPLEMENTATION_SUMMARY.md` before non-trivial changes** — full
architecture, data model, surfaces, and extension patterns live there.
`docs/ORCHESTRATION_ROADMAP.md` has design rationale; `docs/INSTALL_LMS.md`
is the user-facing setup guide.

## Hard invariants

1. ALL writes go through `orchestrator/repository.py` (preserves the
   append-only `issue_events` audit log everything depends on).
2. `state_machine.py`, `pipelines.py`, `adr_rules.py`, `engine/focus.py` stay
   pure — no I/O.
3. The engine never edits or executes repos. Two modes, per gate (`mode:` in
   pipelines.yaml): **verdict** (default) — reasoner/human renders a decision
   in-process; **pull** — a registered `external` worker edits/tests in its OWN
   repo and reports pointers via MCP. The legacy push code-gen + apply/verify
   leg (worktree, `APPLY_ENABLED=false` default, human-only `apply-promote`)
   survives only for the autonomous/stub mode. See `docs/PULL_AGENTS.md`.
4. `off_rails` exits only via `repository.apply_directive` (directive=True).
5. Everything must run hermetically with stub providers (no API keys).
6. Reasoner capabilities are optional/duck-typed — old reasoners keep working.
7. Messages cross team boundaries; issues stay local; responses never re-ingest.

## Working here

- Verify: `service postgresql start` (cloud) or podman/docker Postgres, then
  `.venv/bin/python -m orchestrator.cli migrate && .venv/bin/python -m pytest -q`
  → expect ~141 passed (pgvector test skips without the pgvector image). Never
  run two suites concurrently against one DB (truncation races → phantom failures).
- End-to-end smoke (push/stub): register-agent (dev AND qa per team) → add-goal →
  `run --max-ticks 50` → status: all done, deterministic on stubs.
- Pull mode: `config/roster.example.pull.yaml` + `pull-1` pipeline; register
  `--runtime external` workers; engine assigns + observes; workers drive gates
  via MCP (`list_my_work`/`report_work`/`gate_decision`/`heartbeat`). Pull-gate
  liveness reclaim uses `last_seen` (no new migration added).
- New SQL = new numbered migration (next: 0009). New reasoner op = Protocol +
  stub + Anthropic + optional-capability call. Tests subclass StubReasoner,
  tweak thresholds via `copy.deepcopy(settings)`.
