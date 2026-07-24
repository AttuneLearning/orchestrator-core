# Plan: Durable Token-Aware Workers + Side-Car Watcher

> **Status:** DELIVERED (rev 4, 2026-07-23) — all phases 0-5 implemented, gated,
> committed (tags phase-0..phase-5 on feat/durable-worker-sidecar) and published
> to both workspaces; see §13 delivery record. Soaks (24h phase-1 / 12h side-car)
> in progress post-publish; fleet-wide side-car default flip is gated on them.
> Previously: APPROVED (rev 3, 2026-07-22) — operator gave go-ahead; implementation
> followed the delivery methodology in §11 (model-tiered agent team, phase gates,
> commit per phase, e2e after development).
> Rev 2 folded in review findings: Phase-1 heartbeat/stale sequencing fix, dormant
> status, watchdog-vs-verify ceiling, tick protocol (markers, coalescing, clear
> handshake), side-car poll cadence, role scope, wake dedup, acceptance criteria.
> Rev 3 added Phase 0 (baseline hygiene) and §11 delivery methodology.
> **Scope:** orchestrator repo only (`python-orchestrator-v1`). All launcher/prompt
> changes are made in `templates/project-launchers/**` (canonical source of truth)
> and pushed to project workspaces via `install-launchers`. Engine/dashboard
> changes live in `orchestrator/**`.
> **Supersedes/extends:** `loop-cadence-spec.md` (shipped) — this adds a *push*
> path (side-car injection) the pull-only cadence model explicitly lacked.

---

## 1. Goal & principles

Replace the per-cycle **relaunch** loop (`run-agent-loop.sh` re-execs the agent
every poll) with a **durable agent session** that is driven by a thin, local
**side-car**. Optimize for **least tokens/context per tick**. No process is
relaunched to do normal work; relaunch is reserved for crash/context recovery.

Two kinds of "relaunch", kept distinct:
- **Work loop** — the side-car injects a "tick" prompt into the *same* long-lived
  session. Never relaunches the process.
- **Reasoner relaunch** — a rare janitor: kill+restart the session only on crash
  or when context has grown too large. Time/size triggered, not per-cycle.

## 2. Architecture

```
orchestrator (engine + dashboard)
   │  publishes cadence (loop_enabled, poll_interval_*), wake_at
   ▼
side-car (one per worker, local)            durable worker session (TUI/serve)
   │  - owns cadence (5-min active / dormant)   ▲   - token-gated work
   │  - injects "tick" prompts  ───────────────►│   - drains queue while tokens last
   │  - heartbeat ~20s ALWAYS (HTTP, token-free)│   - memory_write between issues,
   │  - watchdog: kill+restart if stuck         │     then signals READY-TO-CLEAR
   │  - relays orch wake_at as an injected tick │   - yields (turn ends; session persists)
   │  - injects /clear on worker's signal       │
   └─ Ctrl-C exitable
```

**Why the side-car owns cadence (not the worker self-sleeping):** a self-sleeping
worker (blocked in `sleep`) cannot be woken early by injection — only by
kill+restart, which violates "durable, never relaunched." So the worker does a
tick and **yields**; the side-car decides when the next tick fires. This is the
only shape that satisfies *durable session* **and** *on-demand wake*.

**Role scope:** pull workers first — dev workers, QA workers, senior dev/QA
(Phase 2 proves one opencode worker; Phase 5 extends to the rest). Dev-managers
(also `LOOP_AGENT=1`) migrate in Phase 5 with the claude/codex adapters.
**orch-manager is excluded** — it is an interactive supervisory console, not a
pull worker.

## 3. Worker contract (per injected tick)

The worker NEVER sleeps, loops itself, or invokes runtime slash commands
(`/clear`, `/usage` are user-level; the agent cannot run them — the side-car
injects them). Per tick:

1. Read the token budget **from the tick prompt** (the side-car computes and
   embeds it — see §6). Apply a **safety margin**.
2. Not enough to *finish* the next issue → emit `TICK RESULT: NO WORK (insufficient
   tokens)` and yield.
3. Enough → claim + complete the issue. Then **drain**: keep taking issues it can
   finish while budget allows.
4. **Between issues:** `memory_write` a summary of the completed work, then emit
   `TICK RESULT: WORKED #<ids>; READY-TO-CLEAR` and yield. The **side-car** injects
   `/clear`, then injects the next tick immediately (fresh, small context).
5. Otherwise end every tick with a machine-readable marker (see §5 protocol):
   `TICK RESULT: WORKED #<ids>` or `TICK RESULT: NO WORK`.

## 4. Side-car spec

- **Cadence:** tick every **`poll_interval_enabled_seconds`** (dashboard, default
  5 min) while inside a **30-minute active window**; after 30 min with no work,
  drop to **dormant** (1 hr, or until the usage window resets). **Reset the 30-min
  timer whenever a tick did work.** ("Did work" = `TICK RESULT: WORKED …` — not a
  bare heartbeat or a NO-WORK tick.)
- **Own poll cadence (decoupled from worker cadence):** the side-car polls the
  dashboard agent-state every **30–60s ALWAYS** — including while the worker is
  dormant. This is a free HTTP GET and is what makes `wake_at` low-latency; a
  side-car that slept the dormant hour would defeat the wake signal.
- **Heartbeat:** HTTP `POST /agents/$AID/heartbeat` every **~20s ALWAYS** while
  the side-car runs (not just while the worker is progressing — a dormant worker
  must not read as dead). Includes a status field once §7's endpoint extension
  lands: `working | idle | dormant`. Token-free (not a model call).
- **Watchdog:** kill+restart the session (the only relaunch) on:
  - crash (process gone);
  - stuck: no session-output change AND no in-flight MCP call for > T_stuck;
  - tick over **T_max**, where **T_max ≥ 3600s** — MUST exceed the verify ceiling
    (engine `verify_timeout_s` 3000s / client 3300s). A pending `verify_run` MCP
    call is NOT stuck; killing below the verify ceiling re-introduces the
    false-negative bug fixed 2026-07-21 (#2/#3/#43).
  - context too large (context-reclaim; time/size triggered).
  After restart: re-attach/re-discover the session (opencode session id changes
  on restart), then inject a tick immediately so the fresh session resumes work.
- **Wake relay:** store last-seen `wake_at`; trigger **only when it increases**
  (dedup — never re-fire on every poll). On trigger: inject a tick immediately
  and enter the active window.
- **Ctrl-C exitable:** `trap … INT TERM`; clean exit, leaves the durable session
  alive by default (`--kill-worker` to tear down).

## 5. Tick protocol (side-car ⇄ worker)

- **Injection only when idle + coalescing:** a tick can legitimately run 35–55
  min (full-suite verify) while the active cadence is 5 min — collisions are
  guaranteed, not edge cases. The side-car MUST:
  - detect busy before injecting (worker mid-turn → do not inject);
  - keep **at most one pending tick**: if a tick is due while the worker is busy,
    coalesce — deliver a single tick when the worker goes idle, never a backlog.
- **Busy/idle + result detection, per runtime:**
  - *opencode:* `opencode serve` HTTP API — session state and messages are
    readable; post messages via the API (first-class, no keystroke risk).
  - *claude / codex (tmux):* `tmux capture-pane` diff to detect idle-at-prompt
    and to read the `TICK RESULT:` marker; `send-keys` only when idle.
- **End-of-tick marker (worker obligation):** every tick ends with exactly one:
  `TICK RESULT: WORKED #<ids>[; READY-TO-CLEAR]` | `TICK RESULT: NO WORK[ (reason)]`.
  This is the side-car's only signal for the 30-min reset, the clear handshake,
  and stuck detection — the prompt templates must enforce it.
- **Clear handshake:** worker emits `READY-TO-CLEAR` → side-car injects `/clear`
  → side-car injects next tick. The worker never clears itself.

## 6. Token accounting & exhaustion (differs by runtime)

The **side-car owns token accounting** and embeds the budget into each tick
prompt ("you have ≈N% of the window / $ budget remaining"); the worker only
applies the margin. Sources:

- **claude, codex** — time-based usage window. No exact reset timestamp is
  exposed (5h rolling windows), so the side-car tracks usage **heuristically**
  (session accounting + observed limit errors) — do not treat reset time as
  deterministic. On exhaustion: dormant until the estimated window reset, then
  probe with a tick.
- **opencode** — cost/balance (DigitalOcean / open-model API account), **not** a
  timed reset. The serve API exposes real per-session token counts; on
  balance/API errors **alert the human to reload the account** (dashboard alert +
  Correspondence message); do not wait for a rollover that won't come.

## 7. Orchestrator (engine + dashboard) changes

- **Cadence-window fields** (migration): add active-window seconds (default 1800)
  and dormant cadence; the existing `poll_interval_enabled_seconds` (5 min) is the
  active cadence. Operator-editable on `/agents`.
- **Heartbeat status extension:** extend `POST /agents/{id}/heartbeat` (and agent
  state) with an optional status: `working | idle | dormant`. `dormant` is a
  first-class state — **excluded from stale alerting** — so an hour-dormant worker
  doesn't read as dead. (Today the endpoint only bumps `last_seen`.)
- **Wake signal:** per-project `wake_at` timestamp surfaced in the agent-state
  payload the side-car already fetches, plus a trigger (orch-manager after
  promoting work, and a dashboard "Wake all" button).
- **Stale window (sequencing matters — see Phase 1):** target ~**90s**, but ONLY
  after heartbeats are continuous. Note: the config default is
  `agent_stale_seconds: 300` (`config.py`); the 1800 value is a cadencelms-only
  daemon env override. Set the new value in **config/per-instance settings** so
  both projects get it consistently — not just in `start-orch-daemon.sh`. Stale
  agents get their issues **reclaimed** (not merely flagged), so a stale window
  below the worst silent gap causes reclaim churn.
- **Dashboard bind:** set **`DASHBOARD_HOST=0.0.0.0`** (all interfaces + loopback).
  The dashboard currently has **no auth** — accepted deliberately: the operator is
  the sole owner of both the server and the network. Auth to be added later;
  until then this is a known, owner-accepted exposure.

## 8. Distribution (canonical → projects)

- **Canonical source:** `templates/project-launchers/**`. Edit there only.
- **Publish:** `python -m orchestrator.cli install-launchers --workspace <PATH>
  --force` per project (cadencelms, tendcharting).
- **⚠ Gap found during reconciliation:** `install-launchers --force` overwrites
  **every** template file, including per-project-customized env
  (`orchestrator.env` has each project's `DASHBOARD`, `DECOMPOSITION_TIER`,
  qwen-YOLO). A blind `--force` would clobber those. **Fix as part of this work:**
  teach `install-launchers` to skip/merge a declared set of per-project files
  (`orchestrator.env`, `secrets.env`, `*-yolo.env`) instead of overwriting. Until
  then, launcher code is synced surgically (code files only).

## 9. Reconciliation baseline (done 2026-07-22)

Before this plan, real fixes were scattered. Now folded into the canonical
template and synced to both projects (code files only; env preserved):
- verify-timeout MCP client fix (`claude.sh` `MCP_TOOL_TIMEOUT`; `codex.sh`
  `tool_timeout_sec=3300`) — was cadencelms-only, now canonical.
- `CODEX_HOME` per-fleet isolation (`codex.sh`) — was tendcharting-only, now
  canonical (prevents cross-fleet sqlite WAL contention wedging QA).
- `apply_interactive_prompt` + opencode `permission.external_directory` for senior
  roles (`lib.sh` + runtime adapters) — was cadencelms-only, now canonical.

## 10. Phasing (each independently shippable; publish after each)

**Gate rule:** every phase is completed AND functionally tested before the next
begins. Long soaks (24h phase-1, 12h phase-2) run in parallel with the next
phase's development and gate the **fleet-wide publish** step, not phase
progression. Full e2e testing runs after all development completes. **Commit
(and tag `phase-N`) between each phase.**

| Phase | Change (orchestrator repo) | Acceptance | Publish |
|---|---|---|---|
| 0 | Baseline hygiene: commit the ~15 dirty files on `feat/contract-lifecycle-mcp` (verify-timeout engine fix, template reconciliation, this plan) in logical commits; branch `feat/durable-worker-sidecar` | clean `git status`; existing pytest suite green | none |
| 1 | Heartbeat 60→20s AND move it to **loop-lifetime** in `run-agent-loop.sh` (runs through inter-cycle sleeps — today it stops between cycles, so cutting stale first would flag every idle worker and trigger issue reclaim). **Only then** stale → 90s (in config/per-instance for BOTH projects). `DASHBOARD_HOST=0.0.0.0`. | 24h with zero false-stale flags and zero reclaim events on idle-but-alive workers; dashboard reachable on LAN + loopback | push + dashboard restart |
| 2 | Side-car script (cadence, always-on heartbeat, watchdog with T_max ≥ 3600s, tick coalescing, Ctrl-C) + worker-prompt tick contract (markers, clear handshake); prove on ONE opencode worker via `opencode serve` | ≥ 12h run: work completed across ≥ 3 ticks, zero session relaunches for work, one coalesced tick under a long verify, `TICK RESULT` parsed every tick, clean Ctrl-C | push cadencelms, validate |
| 3 | Token accounting in side-car (budget embedded in tick prompt) + memory→READY-TO-CLEAR→`/clear` handshake | worker drains ≥ 2 issues in one active window with a `/clear` between them; context after clear ≪ before; bails cleanly on low budget | push |
| 4 | Engine migration: cadence-window fields + heartbeat `status` (`dormant` excluded from stale alerts) + `wake_at` signal; side-car wake relay (dedup on increase) | dormant worker shows `dormant` not stale; "Wake all" reaches a dormant worker in < 90s | migration + push |
| 5 | claude/codex tmux adapters (capture-pane idle detection); dev-managers migrated; retire per-cycle relaunch for migrated roles; opencode balance-alert; `install-launchers` env-preserve fix | all migrated roles run ≥ 24h on side-car; `install-launchers --force` leaves env files intact (dry-run diff proof) | push both projects |

## 11. Delivery methodology (operator-approved 2026-07-22)

**Agent team (model-tiered):**
- **haiku** — almost all initial coding, driven by tight, fully-specified
  file-level specs with exact anchors and expected diffs (haiku executes narrow
  specs reliably; under-specification is what burns it): Phase-1 heartbeat/config
  edits, migration boilerplate, prompt-template rewrites, `install-launchers`
  env-preserve, balance-alert.
- **sonnet** — complicated coding: side-car state machine (cadence/coalescing/
  watchdog), tmux capture-pane idle detection, engine stale/reclaim integration,
  token heuristics.
- **opus** — sparingly, for the hardest correctness spots (coalescing races,
  reclaim-path change) when sonnet struggles.
- **QA:** sonnet reviews every phase diff; opus gate-reviews the risky phases
  (2 = side-car, 4 = migration/reclaim) and the final e2e.
- The orch-manager session is the integrator: writes specs, reviews, commits.

**Reliability practices:**
1. **Fake-worker stub** (script that emits `TICK RESULT:` markers on cue) so the
   entire side-car state machine — coalescing, 30-min reset, watchdog, wake
   dedup — is tested deterministically and token-free before any live agent runs.
2. **Scratch orchestrator instance** (`--instance sandbox`) for stale/reclaim
   experiments — never the live cadencelms fleet; a mis-tuned stale window would
   reclaim real in-progress issues.
3. **Existing pytest suite runs before AND after every phase** (regression net
   for engine changes).
4. **DB backup before the Phase-4 migration** (`sync-backups.sh` →
   orchestrator-backups repo); tag each phase commit (`phase-N`) for
   one-command rollback.
5. **Publish choreography:** pause worker loops → `install-launchers` push →
   relaunch. Never hot-swap launcher scripts under running loops.

## 12. Open items / risks

- **Token estimation is a heuristic** (accepted): side-car accounting + margin;
  may bail before an issue it could have done, but never mid-commit. Claude/codex
  window-reset time is estimated, not exact.
- **tmux inject fragility** for claude/codex: capture-pane idle-detection is the
  mitigation; inject only at an idle prompt, coalesce to one pending tick.
  opencode's API path is cleaner — prefer `opencode serve` where possible.
- **Context persistence across ticks** relies on the clear handshake; the
  side-car context-age restart is the backstop if a worker stops signalling
  READY-TO-CLEAR.
- **Watchdog tuning:** T_max must track the verify ceiling if `verify_timeout_s`
  is ever raised — derive it from settings (ceiling + margin), don't hardcode.
- **`install-launchers` env-clobber** must be fixed before any `--force` push
  (Phase 5), or per-project env is destroyed.
- **Dashboard auth** deliberately deferred (owner-accepted, single-owner
  server/network); revisit when adding any additional operator or exposure.

## 13. Delivery record (2026-07-23)

Branch `feat/durable-worker-sidecar`, tags `phase-0`..`phase-5`. Suite grew
906 → 1086 tests, all green at every phase boundary.

| Phase | Commit | Gate |
|---|---|---|
| 0 | baseline commits + branch | pytest 906 green |
| 1 | e4b5ecb loop-lifetime 20s heartbeat; stale 120s (raised from plan's 90 per QA headroom finding); dashboard 0.0.0.0 verified | sonnet QA + functional heartbeat test (stub dashboard: continuous beats through sleeps, no orphan) |
| 2 | 21d1e8c sidecar.py + tick-contract.md (opencode) | opus gate ×3 (FAIL→fix→FAIL→fix→PASS): async-inject stale-read baseline, non-fatal HTTP, alive debounce, session ownership by title, suppression choke point, policy coercion, t_max 5100, restart-storm + baseline-sentinel corner cases. Live e2e vs real opencode serve |
| 3 | 9f1ce06 TokenAccountant, CONTEXT BUDGET ticks, FORCED_CLEAR backstop | sonnet QA (FAIL→fix): cache.write/reasoning counted, monotonic cost, limit validation. Live drain e2e: session rotation + fresh-context budget verified via opencode API |
| 4 | 3f4a8f5 migration 0024 (cadence fields, wake_signal), heartbeat status working/idle/dormant, wake relay, /agents wake button, MCP heartbeat status | opus gate PASS w/ follow-ups applied (wake key symmetry via registry-resolved key; migrate-before-code documented in migration header). DB backups refreshed first (sync-backups.sh) |
| 5 | e376bcb TmuxAdapter (claude/codex), balance alert → pause + /alerts Correspondence, AGENT_SIDECAR=1 launch mode, --print-cmd (env-self-contained, COMMAND_TIMEOUT=0) | sonnet QA (FAIL→fix, 8 findings incl. marker truncation monotonicity, per-project port/buffer namespacing + session-directory ownership check, drain /clear idle-gating, restart cooldown 120s). Fake-TUI tmux e2e (found alive()/restart-storm defects live) |
| — | 22f92f4 install-launchers env-preserve + atomic writes (pulled forward from phase 5) | proven at publish: env checksums byte-identical through --force |

**Publish/cutover executed (2026-07-23, in order):** DB backups → install-launchers
--force to both workspaces (env preserved, verified by checksum) → `migrate` both
instances (0024 applied) → dashboard restart (new payload/endpoints live on
0.0.0.0:8800) → full worker-loop relaunch both fleets via respawn-pane (new 20s
loop-lifetime heartbeat live) → daemon restart both instances with NO stale env
override (stale=120 active) → cadencelms agent-1 (backend dev, opencode)
relaunched under AGENT_SIDECAR=1 as the live side-car soak worker.

**Still open (soak-gated):** 24h zero-false-stale observation; ≥12h side-car
worker soak (work across ≥3 ticks, clear handshake, no relaunch-for-work);
claude/codex tmux side-car mode used in anger (needs SIDECAR_TMUX_TARGET);
fleet-wide AGENT_SIDECAR default flip after soaks. Known accepted heuristics:
tmux idle detection vs animated TUIs (marker-stability collection +
IDLE_FALLBACK bound the damage), token estimation (§12).

## 14. Post-delivery defects (first soak, 2026-07-23)

First live soak: cadencelms agent-1 (backend dev, opencode) under
`AGENT_SIDECAR=1`, ran ~6h14m (02:23→08:38 MST) before a manual `^C`
(clean `SHUTDOWN kill_worker_on_exit=False`). **Durability held** — session
alive the whole window, continuous heartbeat (505 STATE_POLL), zero STALE,
zero crash, zero relaunch-for-work. **Work path failed** — all 3 ticks
`TICK_RESULT valid=False` (violations 1→2→3), one `FORCED_CLEAR reason=context`
instead of a clean READY-TO-CLEAR handshake. Soak does NOT count; the 6h are
void. Two defects + a test gap, both integration-seam bugs the phase QA missed
because tests stubbed the adapter and the injected prompt.

**DEFECT-SIDECAR-1 — injected prompt carries no tick contract (every tick invalid).**
`parse_tick_result` (`sidecar.py:172`) requires a `TICK RESULT:` marker
(`_MARKER_RE`, line 148). The grammar lives in
`agent-launchers/prompts/tick-contract.md` but never reaches the worker:
`start-agent.sh:185-188` builds the sidecar `--prompt-file` from the role
prompt only (`render_prompt "$PROMPT_FILE"`), never concatenating the contract.
Verified live: `grep -c -i "tick result" /tmp/sidecar-prompt-cadencelms-1.*` → 0.
The worker ran the legacy "print NO WORK and stop" prompt, never told to emit
`TICK RESULT: …`, so every tick parsed garbled → `valid=False`.
_Fix:_ in the sidecar branch of `start-agent.sh`, append the rendered
`prompts/tick-contract.md` to `RENDERED_PROMPT` before writing
`$SIDECAR_PROMPT_FILE` (contract file already exists and matches the parser
grammar). Canonical template edit → `install-launchers --force` → relaunch.
_Status: **FIXED in template (pending publish)**, 2026-07-23._ Applied to
`templates/project-launchers/start-agent.sh` (renders + appends the contract,
hard-fails if the contract file is missing). Regression added in
`tests/test_project_bootstrap.py::test_agent_sidecar_opencode_routes_through_sidecar_py`
asserting the injected prompt contains the contract block + `TICK RESULT:
WORKED`/`NO WORK` grammar. Suite green (156 in bootstrap+sidecar). Not yet
committed, not yet published to workspaces — rides the same
`install-launchers --force` + relaunch as DEFECT-SIDECAR-2.

**DEFECT-SIDECAR-2 — cumulative session total read as current context size (1589%).**
`OpencodeAdapter.get_usage()` (`sidecar.py:644`) reads `GET /session/{id}`,
whose `tokens.total` is cumulative-per-session (its own comment, line 645) — it
only grows, and is NOT current context-window occupancy. `TokenAccountant`
divides by `context_limit_tokens` (180k) → tick 2 logged
`context_tokens=2,860,186 pct=1589.0`, tripping the FORCED_CLEAR backstop. Tell:
the two 19k/10.6% readings were fresh sessions; the 2.86M was the SAME session
after a work cycle.
_Fix:_ derive occupancy from the latest turn, not the session aggregate — read
`_last_completed_assistant_message()`'s per-message `info.tokens`
(input + output + reasoning + cache.read + cache.write) as `context_tokens`;
keep `GET /session` only for cumulative `session_cost` if wanted. Confirm
per-message `info.tokens` shape against `oc-api-cheatsheet.md` before writing.
_Status: **FIXED in template (pending publish)**, 2026-07-23._ Rewrote
`OpencodeAdapter.get_usage()` to read the last completed assistant turn's
`info.tokens` (via `_last_completed_assistant_message()`, `/message`) through a
new `_context_tokens_from()` helper that keeps the Phase-3 sum/prefer-total
math — only the SOURCE changed from session-cumulative to per-turn; cost still
from `GET /session`. Returns None (unknown budget) when no turn has completed
yet (post-clear) instead of a bogus 0. NB: `oc-api-cheatsheet.md` does not
exist in-repo (only referenced in comments); shape confirmed from the
code/test-encoded `tokens{input,output,reasoning,total,cache{read,write}}`.
Regression tests added to `tests/test_sidecar.py`: last-turn-not-cumulative
(session carries the 2.86M trap, reading must be 19051), post-clear→None, and
the three existing get_usage tests updated to the per-turn source. Suite green
(158 in bootstrap+sidecar, +2). Not committed, not published.

**TEST-GAP — both bugs slipped Phase 2/3 QA because the seams were stubbed.**
`parse_tick_result` tests fed synthetic text containing the marker (passed while
the real prompt produced none); the token test fed a synthetic single usage
dict (never exercised cumulative-vs-per-turn). Add: (a) a test that renders the
actual injected sidecar prompt and asserts it contains the `TICK RESULT`
contract; (b) a multi-turn session fixture asserting `context_tokens` tracks the
last turn, not the running total.

**Re-soak:** after both fixes land + tests + republish, restart the agent-1
side-car worker and reset the clock from zero (prior 6h void). Operator elected
a **6h** side-car soak for this cycle (2026-07-23), not the plan's ≥12h — the
observation window is shortened by operator decision; the pass criteria
(work across ≥3 ticks all valid, clean READY-TO-CLEAR handshake, no
relaunch-for-work, no false-stale) are unchanged.

## 15. Design change — orchestrator-authoritative work signal (2026-07-23)

**Status:** **DELIVERED 2026-07-24** — Phase A (`fedc5ee` engine work signal),
Phase B (`8f12416` side-car consumes it), Phase C (this commit — marker demoted
to advisory in tick-contract.md + parser docstring). 173 tests green; deployed
live (dashboard restarted, fleet relaunched) and validated in production: first
`WORK_SIGNAL` fired on fe-qa (codex) with `TICK_RESULT=0` — work credited with
no marker emitted. Supersedes the
"parser fallback" (option 1) floated against DEFECT-SIDECAR-1's compliance
sub-issue. Target branch `feat/durable-worker-sidecar`.

### 15.1 Problem this solves
DEFECT-SIDECAR-1's fix (injecting `tick-contract.md`) delivers the contract, but
two opencode models proved they will not reliably emit the required
`TICK RESULT: WORKED #<id>` line: glm-5.2 (first soak) and deepseek-4-flash
(re-soak, 5/5 ticks `valid=False`, violations 1→5, one FORCED_CLEAR, then
DORMANT). Only claude complied. Because the side-car learns "did work happen"
ONLY by text-parsing the worker's reply, a non-compliant model leaves it blind:
`last_worked_at` never advances → spurious `window_elapsed` dormancy; context
only ever cleared by the FORCED_CLEAR backstop, never a clean handshake. The
marker is a fragile, model-dependent channel for information the orchestrator
**already holds authoritatively.**

### 15.2 Key realization — the info already exists at the orchestrator
The worker reports work DIRECTLY to the orchestrator via MCP, independent of the
side-car: `claim_issue`, `report_work`, `gate_decision`. Those land as recorded
events (`_append_event` in `repository.py`: `code_committed`, `tests_run`, gate
outcomes) on issues whose owner is the agent (`claim_issue` stamps
`claimed_by=agent_id`). So "did agent N do real work since time T" is a
server-side query over EXISTING data — no new worker behavior, no schema change.

The `TICK RESULT` marker duplicates, in fragile reply-text, what the worker
already tells the orchestrator over a transactional MCP channel. The fix is to
stop parsing the duplicate and **have the side-car ASK the orchestrator.**

### 15.3 Design — side-car ← orchestrator (poll, not relay)
Invert the dependency. Do NOT route work through the side-car
(worker→side-car→orchestrator would be a fragile step backward from the direct
MCP path). Instead the side-car READS the orchestrator's authoritative record:

- **Server (dashboard):** extend the state-poll response the side-car already
  fetches — `GET /agents/{agent_id}/pause?project=…` (see `sidecar.py`
  DashboardClient, `dashboard/app.py:902`) — with a monotonic per-agent work
  signal, e.g. `last_work_at` (max `created_at` of work events on issues owned
  by the agent) or a `work_seq` counter. "Work event" = `code_committed` /
  gate `passed` / green `tests_run` / `report_work`. Bounded to a recent window;
  it is derived, so **no migration** is required.
- **Side-car:** in `_maybe_poll_state`, compare the new `last_work_at`/`work_seq`
  against the value from the prior poll. If it advanced → treat as an
  authoritative WORKED signal: `last_worked_at = now`, and flip DORMANT→ACTIVE
  (exactly what a valid `TICK RESULT: WORKED` does today at `sidecar.py:1948`).
  This decouples the window/cadence decision from the worker's reply formatting.

### 15.4 What stays marker-driven (and why it's fine)
`READY-TO-CLEAR` (early, worker-chosen context reset) is inherently local — the
orchestrator can't know the worker's context window is filling. But that path is
ALREADY backstopped by the TokenAccountant + `FORCED_CLEAR` (Phase 3), which is
side-car-owned and needs no marker. So after 15.3, the `TICK RESULT` marker
becomes **advisory, not load-bearing**: a complying model (claude) still gets a
clean early clear via the marker; a non-complying model (opencode open-source)
degrades gracefully to orchestrator-signalled work + token-backstop clears.
Keep parsing the marker when present; never depend on it for correctness.

### 15.5 Phasing
- **Phase A — server signal.** Add the per-agent work signal to the state-poll
  payload (dashboard route + repository query). Unit-test the query
  (work events on owned issues since T; ignores non-work events; monotonic).
- **Phase B — side-car consumption.** Consume the signal in `_maybe_poll_state`;
  advance `last_worked_at` / wake on increase. Unit-test in `test_sidecar.py`
  with a fake dashboard whose work signal advances mid-run: assert the window
  does NOT elapse and DORMANT→ACTIVE fires, WITHOUT any valid `TICK RESULT`.
- **Phase C — demote the marker.** Update `tick-contract.md` and comments to
  describe `TICK RESULT` as advisory; confirm a fully non-emitting worker still
  soaks cleanly (window kept alive by the orchestrator signal, clears by the
  token backstop).

### 15.6 Risks / open questions
- **Attribution precision:** if a work event's owning-agent mapping is stale
  (issue reassigned mid-tick), the signal could mis-credit. Mitigate by keying
  on the event's own actor/`claimed_by` at event time rather than current owner.
- **Latency:** `report_work` may land just after the tick returns; the next
  ~45s state poll picks it up — acceptable for cadence, which operates on
  minutes.
- **Double-count harmlessness:** if BOTH a valid marker and the orchestrator
  signal fire for one tick, both just set `last_worked_at = now` — idempotent.
- **New coupling:** the side-car now depends on the dashboard exposing the
  signal; degrade safely (signal absent/unreachable → fall back to today's
  marker-only behavior, never crash — same tolerance as the existing poll).
