# Spec: Per-Agent Loop Cadence Control (dashboard switch + heartbeat-driven cadence)

**Audience:** orchestrator dev agent (engine + dashboard codebase — NOT this UI repo).
**Author:** agent-4 (frontend dev), on behalf of the operator.
**Status:** proposed — implement in the orchestrator codebase.
**Related:** ADR-ORCH-001 (must be refined — see §7).

---

## 1. Goal

Give the operator per-agent control over how often each pull worker polls, from the
dashboard `/agents` page, to manage token spend. A worker that is "loop enabled" keeps
looping at a fast cadence (default **5 min**) even after its queue empties; a worker that
is "loop disabled" does **not** stop — it keeps polling at a slow cadence (default
**20 min**). Both cadences are per-agent customizable from the dashboard.

The orchestrator is a **pull** model: it cannot push a wake or force a sleep into a running
CLI agent. Therefore the dashboard switch only *publishes desired cadence*; the worker
*reads it on every poll* (via `heartbeat`) and self-paces. See the worker-side scaffold:
`docs/orchestrator/worker-loop-runbook.md` in the UI repo.

## 2. Agent registry schema changes

Add to the agent registry (the table backing `get_status().agents` and the `/agents` page):

| Column | Type | Default | Notes |
|---|---|---|---|
| `loop_enabled` | bool | `false` | Token-safe default: new agents do NOT loop hot until turned on. |
| `poll_interval_enabled_seconds` | int | `300` (5 min) | Idle cadence when `loop_enabled = true`. Operator-editable. |
| `poll_interval_disabled_seconds` | int | `1200` (20 min) | Idle cadence when `loop_enabled = false`. Operator-editable. |

Validation bounds (reject out-of-range writes): `60 ≤ interval ≤ 7200`. Recommend a soft
warning in the UI when `poll_interval_enabled_seconds > 300` (5 min), because crossing the
**5-minute Anthropic prompt-cache TTL** makes each idle poll pay a full uncached context
re-read — see §6.

## 3. Cadence resolution (server-side)

The server computes the next idle wait for an agent:

```
next_poll_seconds =
    poll_interval_enabled_seconds   if loop_enabled
    else poll_interval_disabled_seconds
```

This value governs **idle** waits only. When a worker has assigned work it processes the
queue back-to-back (no sleep between issues); `next_poll_seconds` applies after the queue
drains. The worker never sleeps while it still has implementation-gate work.

## 4. `heartbeat` payload contract (the key change)

`heartbeat(agent_id)` currently returns `{ "status": "ok" }`. Extend it to return the
agent's live loop policy so the worker fetches cadence on the same call it already makes
every poll:

```jsonc
// heartbeat(agent_id) ->
{
  "status": "ok",
  "loop_enabled": true,
  "next_poll_seconds": 300        // already resolved per §3; worker just obeys it
}
```

Rationale: the worker calls `heartbeat` on every poll and during long work, so it always
sees the latest operator setting within at most one interval. No new tool needed; no
worker-side hardcoded numbers. Toggle latency: flipping the switch while the agent is
mid-sleep takes effect on its next wake (acceptable for a throttle).

`get_status().agents[]` and any single-agent fetch should also include `loop_enabled`,
`poll_interval_enabled_seconds`, `poll_interval_disabled_seconds` so the dashboard can
render current state.

## 5. Dashboard `/agents` page UI

Per agent row in the registry listing, next to the existing status:

- **Loop toggle** — switch bound to `loop_enabled`. Writes immediately.
- **Two editable interval inputs** beside the switch:
  - "Enabled poll" (minutes, default 5) → `poll_interval_enabled_seconds`
  - "Disabled poll" (minutes, default 20) → `poll_interval_disabled_seconds`
- Inputs accept minutes (convert to seconds on write), enforce the §2 bounds, and show the
  >5-min cache-cost warning on the enabled field.
- Optional: show `next_poll_seconds` / "next poll in ~Xm" as read-only feedback.

Persist changes to the registry; they take effect on the agent's next `heartbeat`.

## 6. Token / cache rationale (why these defaults)

- An **idle** poll's cost is dominated by re-reading the agent's context. The Anthropic
  prompt-cache TTL is **5 minutes**.
- **Enabled = 5 min** sits at the cache boundary → idle polls stay (mostly) cache-warm and
  cheap, while staying responsive.
- **Disabled = 20 min** intentionally busts the cache per wake (pricier per wake) but does
  ~4× fewer wakes → net token savings during long idle stretches, while keeping the agent
  reachable within ~20 min.
- A persistent looping agent costs tokens on **every** wake regardless of cadence. If
  zero-idle-cost is ever required, that's a separate "cron spins a fresh CLI per poll"
  design (out of scope here).

## 7. ADR (live rule)

ADR-ORCH-002 (proposed, supersedes ADR-ORCH-001) is the loop/cadence rule. Correct decision:

> Clear every assigned implementation-gate issue without pausing — including QA/verification
> rejections that bounce back as implementation work — each: implement+test, commit (never
> push), report_work, gate_decision(passed=true); repeat until gates pass. On empty queue,
> obey heartbeat next_poll_seconds (loop_enabled→fast cadence + take new work; else slow-poll
> — never fully stop).

Fixing QA rejections is the loop's **purpose**, not an opt-in: a rejected issue returns to the
worker as implementation-gate work and is cleared on a later cycle until gates pass.

**Two engine fixes needed (orchestrator dev):**
1. The row stored as ADR-ORCH-002 still has stale text containing a "never auto-fix QA
   rejections unless enabled" carve-out — replace it with the decision above (that carve-out
   was a temporary testing directive, not the rule).
2. `adr_create` key generation collides on supersede: it mints an already-used
   `ADR-ORCH-NNN` instead of the next free number, and there is no `adr_update` tool — so a
   superseding/corrected ADR currently cannot be created or edited. Fix the numbering (derive
   next NNN from max existing key, not visible-count) and/or add `adr_update`.

Then `adr_approve("ADR-ORCH-002")`.

## 8. Single source of truth + vendor doc sync (REQUIRED)

The canonical worker protocol (gates owned, no-push, report→gate sequence, the loop/cadence
semantics, QA-repair opt-in) must live in the **orchestrator codebase** as the single source
of truth — served to any client (Claude or OpenAI) via `adr_list` / `context_load`. The
per-vendor instruction files in each worker repo are **generated artifacts**, not
hand-authored:

- Build a render step (CLI subcommand or MCP tool), e.g.
  `orch render-agent-docs --repo <name>`, that pulls accepted ADRs + the protocol template
  for that repo/team/agent and writes:
  - `CLAUDE.md` (Claude Code)
  - `AGENTS.md` (OpenAI Codex / cross-tool convention)
  Both get a generated header: `<!-- GENERATED FROM ORCHESTRATOR SoT — DO NOT EDIT BY HAND -->`
  and identical protocol bodies (vendor-specific bootstrap differs only in the entry line).
- Build a check step (CI / pre-engage): re-render in memory and compare to the committed
  files (hash/diff). Fail if drifted ("CLAUDE.md/AGENTS.md out of sync with orchestrator SoT;
  run render"). This is the "checked and updated from the single source of truth" requirement,
  and is the vendor-neutral form of the CLAUDE.md-completeness check (completeness ==
  matches-generated-from-SoT).
- Optionally gate work: `list_my_work`/`claim_issue` may require the agent to have called
  `context_load` (acknowledged current protocol + ADR version) before returning work — the
  truly "any cli/agent" enforcement point, since the server can't read a client's local file.

## 9. Out of scope / non-goals

- The worker-side loop driver itself (lives in the worker repo; see worker-loop-runbook.md).
- Zero-idle-cost / cron-spawned ephemeral agents.
- Pushing wakes to agents (incompatible with the pull model).

## 10. Acceptance criteria

- [ ] Registry has `loop_enabled` (default false) + the two interval columns (defaults 300/1200), with §2 bounds.
- [ ] `heartbeat` returns `{ status, loop_enabled, next_poll_seconds }`; `get_status` agents include the loop fields.
- [ ] `/agents` page shows a per-agent toggle + two editable minute inputs with validation and the cache warning; writes persist and take effect on next heartbeat.
- [ ] ADR-ORCH-001 refined per §7 and approved.
- [ ] Render + check tooling produces and validates `CLAUDE.md` + `AGENTS.md` from the orchestrator SoT (§8).
