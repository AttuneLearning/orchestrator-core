# Orchestrator Architecture

The system-of-record for how this orchestrator works: components, lifecycle,
gate ownership, tool surfaces, and the guardrail map. **Load this instead of
re-deriving the system from source** — it is written so a mid-tier model holding
the orch-manager seat (or any fresh session) can operate safely from it.

Written 2026-07-12 against the post-guardrails codebase (G1–G9, GAP-1..6).

---

## 1. Components

| Component | Entry point | Role |
|---|---|---|
| **Engine (daemon)** | `orchestrator.cli run --daemon` → `engine/loop.py::Engine.tick` | The only autonomous mover. Ingests messages, decomposes goals, assigns/advances issues, renders lead-gate verdicts, auto-promotes done work. |
| **MCP server** | `orchestrator.cli serve` → `mcp_server/server.py::build_server` | Per-worker-session stdio server; the ONLY interface pull workers have. Tool modules: `tools_issues`, `tools_skills` (ADR/comms/memory), `tools_contracts`, `tools_status`, `tools_docs`. |
| **Dashboard** | `orchestrator.cli serve-dashboard` (`:8800`) | Human review surface: `/adrs` proposal queue, `/contracts` review, `/orch/monitor` inbox, goals/issues/agents/workers pages, settings. |
| **CLI** | `orchestrator.cli <cmd>` | Human directives: `status`, `adr approve`, `apply-promote`, `cancel`, `directive resume`, `goal-promote/reject`, `migrate`, `render-agent-docs`. |
| **Coordinator DB** | Postgres (per instance, `config/instances.yaml`) | Single source of truth: goals, issues, `issue_events` (append-only), agents, adrs, contracts, messages, memory_notes, docs, `issue_adrs`. |
| **Reasoner** | `agents/reasoning.py::make_reasoner` | Engine-side LLM for decisions: `stub | anthropic | openai | cli` (cli = local coder CLI, e.g. codex via a `{prompt}` command; retries → `ReasonerExhausted` = PAUSE, never a gate decline). |

**Instances**: `--instance <key>` / `ORCH_INSTANCE` selects DB + roster + settings
overrides from `config/instances.yaml`. Always pass the instance-resolved
settings through (`tools_issues.register(mcp, pool, settings)`), never the defaults.

## 2. Issue lifecycle

**States**: `ready → in_progress → done`, plus `blocked` (deps/contracts),
`failed` (retry cap), `cancelled` (triage; terminal), `off_rails` (quarantine
latch; only a human `directive resume` exits it).

**Engine tick order** (`Engine.tick`): `_ingest` → `_decompose` → `_unblock` →
`_assign` → `_advance` (work + review steps) → `_reclaim` (stale workers) →
`_sweep` → `_reengage` → `_reconcile`.

- **_ingest**: pending `request` messages → reasoner triage → goal+issue.
  Messages to `orchestration` (MONITOR_TEAMS) are NEVER auto-decomposed — they
  wait for a human on `/orch/monitor` and in `status`.
- **_decompose**: reasoner splits a goal into issue specs (QA-duplicate specs
  dropped; caps enforced via alerts+goal pause, no silent truncation). At
  creation each issue gets: team from the pipeline, work_type detection, the
  reasoner's ADR tags (`issue_adrs`, best-effort), G6 senior routing for
  architecture/security-heavy specs, G5 multi-deliverable sizing flag.
- **_advance / review**: worker-owned gates wait for pull workers; lead-owned
  gates are decided by the engine reasoner. `intake`, `contract_check`, and
  `completion` auto-pass (the substantive verdicts live at verification/qa_gate).
  On DONE: auto-promote (below) + `_maybe_suggest_adr` when no rules governed.
  On decline at/past `escalate_to_senior_at` (default 2): reassign to senior (G8).

## 3. Pipelines & gate ownership (the part people get wrong)

Defined in `config/pipelines.yaml`. The pull pipelines (`pull-1`, `pull-fe`, `pull-be`):

```
intake(lead) → [contract_check(lead), pull-fe] → implementation(dev worker)
      → verification(qa worker) → qa_gate(lead/engine) → completion(lead/engine)
```

**Worker-owned** gates (`implementation`, `verification`) advance only when an
external pull worker reports + gate_decisions. **Lead-owned** gates (`intake`,
`contract_check`, `qa_gate`, `completion`) advance only when the ENGINE ticks —
if the daemon is stopped, issues park at `qa_gate` even though QA "passed".
That is expected, not a bug.

**Verification vs acceptance**: per-issue QA = typecheck + unit tests (now via
`verify_run`). Playwright/e2e is the GLOBAL acceptance gate on merged main —
never spawn per-goal "acceptance failed" fix issues from it.

## 4. Agents, worktrees, write scopes

Roster (`config/roster.<instance>.yaml`) registers numbered agents. tendcharting:

| # | team/function | worktree | write scope (GAP-1, enforced) |
|---|---|---|---|
| 1 | backend/dev | wt-backend-dev | `apps/api/`, `packages/contracts/`, `contracts.seed.json` |
| 2 | backend/qa | wt-backend-qa | none — QA never commits |
| 3 | frontend/dev | wt-frontend-dev | `apps/web/` only (contracts read-only → `contract_propose`) |
| 4 | frontend/qa | wt-frontend-qa | none |
| 5,7 | senior/dev | wt-seniordev | everything (escalation lane; issues KEEP their own team) |
| 6 | senior/qa | wt-senior-qa | everything |
| — | orch-manager | workspace root | coordinator state via MCP only; never product code |

Enforced twice: per-worktree `pre-commit` hooks (`<workspace>/.worktree-hooks/`,
via `extensions.worktreeConfig`) fail fast locally; the coordinator's
`_verify_commit_real` re-checks at report time (hooks are bypassable, the
coordinator gate is not). The claim team-guard blocks cross-team claims;
`senior` is exempt.

**Promotion**: workers NEVER merge/push. On DONE the engine merges
`issue-<id>` → `promote_branch` locally (`apply/worktree.py::auto_promote_on_done`;
conflict = complete-and-log + orch-monitor message, lockfile conflicts
auto-healed). Human path: `apply-promote` (requires a passed verification event).
Nothing ever pushes to a remote.

## 5. Worker MCP tool surface (what a pull worker may do)

Read/scoped: `heartbeat`, `list_my_work`, `list_issues`, `get_issue`,
`adr_for_issue` (NOT `adr_list` — scoped: reasoner tags ∪ team selectors ∪ full
forward backlink closure), `contracts_for_issue` (NOT `contract_list`),
`context_load`, `comms_check/read`.

Write (all gated): `claim_issue` (readiness soft-flag), `report_work` (G1/G7/
GAP-1 verified), `verify_run` (harness-executed), `gate_decision` (evidence-
required), `comms_send`, `adr_suggest` (dedup + rate limit), `contract_propose`
(rate limit), `create_subissue`, `append_log`.

**Removed from workers on purpose** (self-approval holes that produced ~1,500
junk ADRs): `adr_create`, `adr_approve`, `adr_update`, `contract_agree`,
`contract_upsert`. Approval is human-only: dashboard `/adrs` + `/contracts`, or
CLI `adr approve`.

## 6. Guardrail map (what blocks bad work, and where)

| Guardrail | Where | What it stops |
|---|---|---|
| G1 real-commit | `tools_issues._verify_commit_real` | empty-sha/"claimed done" reports (was 62% of commits) |
| GAP-1 lanes | same + `.worktree-hooks/` | out-of-lane/misplaced work |
| G7 diff lints | same | placeholder tests, raw `fetch(` in web components |
| GAP-6 seed warn | `report_work` | silent contract-surface drift (soft) |
| GAP-4 verify_run | `tools_issues.verify_run` + verification gate | self-reported test passes |
| G4 readiness flag | `claim_issue` | vague specs (soft — logs `readiness_warning`) |
| G8 escalation | engine review step | blind retry-to-exhaustion (→ senior at 2 declines) |
| G5/G6 routing | engine `_decompose` | oversized specs (flag) / architecture work on small models (→ senior) |
| G2 suggestion locks | `adr_suggest`/`contract_propose` rate limits | governance spam loops |
| G3 wrapper breaker | `run-agent-loop.sh` (workspace) | hung cycles (timeout) + identical-output wedges (2h pause) |
| G9 reasoner resilience | `CliReasoner._ask` retries → `ReasonerExhausted` | transport errors becoming gate declines (was 972 events) |
| GAP-5 stamps | report/gate/verify payloads | untraceable per-tier performance (agent_id/runtime stamped server-side) |

## 6b. ADR graph rules (authoring new ADRs)

The per-issue pull (`adr_for_issue`) is: reasoner tags ∪ universal floor (no-team,
no-work_type rules) — or the team-selector fallback when untagged — expanded via
**forward-only** closure over `related` edges. Keep the graph healthy:

1. **`decision` is the only field agents ever see** — write it as a token-minimal
   directive; rationale/examples/history go in `context` (human-side, free).
2. **Every team-scoped ADR must be reachable** from its flow hub
   (`ADR-FLOW-BE/FE/SEC/BILL-001` star graphs: hub → members; exclusive members →
   hub). An unlinked team-scoped ADR can silently vanish on tagged issues.
3. **Universal ADRs must NOT forward-link into team-scoped clusters** — a
   universal rule is in every floor, so such an edge drags a whole cluster into
   every surface (found live: DEV-001→DEV-002 pulled the entire FE cluster into
   backend pulls).
4. **Shared cluster members (in >1 hub) get NO backedge** — a backedge from a
   shared member would union its clusters on every tag of it.
5. Keep `work_types` selectors empty unless certain: selectors intersect, so a
   work_type restriction silently excludes the rule from adjacent work.

## 7. Invariants (never violate)

1. Workers never push, merge, or promote; the engine owns integration.
2. Workers never approve governance (ADRs/contracts); humans do.
3. Lead gates move only when the engine runs — check the daemon before debugging "stuck" issues.
4. `orchestration`-team messages are for humans; the engine must not auto-act on them.
5. E2E acceptance is global on merged main, never per-issue/per-goal.
6. Reasoner transport failure = pause/quarantine, never a gate verdict.
7. Every trust decision is mechanical: a model's claim is admitted only through a gate that verified it.

## 8. Model tiers (ADR-PROC-003)

T1 large (Claude/Opus, gpt-5.4): decomposition, architecture/security, gate
verdicts, senior lane. T2 mid (gpt-5.4-mini, glm, haiku): contract-bound
single-lane implementation, QA orchestration via `verify_run`. T3 local
(qwen3-coder): single-file, contract-bound, test-guarded backend CRUD only.
Promotion/demotion per lane on GAP-5 telemetry. All tiers untrusted by default —
the gates in §6 are the admission control.
