# FANOUT-CLAUDE.md — agent fanout charter for the Workflow Profile build

This file scopes the multi-model development fanout that implements
`WORKFLOW-PROFILE-TASKS.md` (which implements `WORKFLOW-PROFILE-IMPLEMENTATION-PLAN.md`).
Every spawned agent receives this file in its prompt and MUST follow it, in addition to the
project's `CLAUDE.md` (hard invariants there always apply).

> Named FANOUT-CLAUDE.md (not CLAUDE.md) so it never shadows the project instructions file;
> the orchestrator injects it into every subagent prompt explicitly.

---

## 1. Chain of command (model ladder)

| Tier | Model | Role |
|---|---|---|
| T0 | **haiku** | Default executor. Runs every WP not pre-marked otherwise. Cheap, fast, plentiful. |
| T1 | **sonnet** | (a) Pre-marked complex WPs (`Tier: sonnet` in the tasks doc); (b) rescue of any WP haiku failed twice. |
| T2 | **opus** | (a) QA reviewer at every phase gate and final review; (b) rescue of any WP sonnet failed twice. Opus writes code ONLY in rescue mode — QA mode is read/report only. |
| T3 | **fable** (the orchestrating session) | Monitor. Dispatches waves, serializes DB test runs, runs gates and end-to-end verification, commits at gates. Writes code ONLY when opus has failed a rescue twice — the last resort. |

**Target distribution:** most WPs land at haiku (in the tasks doc: 15 of 22 coded WPs are
haiku-tier). Sonnet exists for the pre-marked cross-cutting items and rescues. Escalation is
mandatory and mechanical — no tier "tries one more time" past its budget.

## 2. Escalation protocol

A WP attempt **fails** when any of: a checklist item cannot be truthfully checked; the WP's
listed tests are red after the agent's own fixes; the agent reports `BLOCKED`; the agent's diff
touches files outside the WP's `Files:` list (scope breach = automatic fail, revert).

- Each tier gets **max 2 attempts** (initial + one retry with its own failure notes).
- On the 2nd failure, the monitor escalates one tier up with a **handoff packet**:
  the WP text · the failed attempt's diff (or its revert note) · the failure report ·
  test output tails. The higher tier starts from the packet, not from scratch.
- `WP-13` is pre-flagged: escalate sonnet→opus after the FIRST failure (state-machine adjacent).
- Anything reaching fable is a design problem, not a coding problem — fable may amend the plan
  document (with a changelog note) rather than brute-force the code.

## 3. Rules every coding agent obeys

1. **Scope:** touch ONLY the files in your WP's `Files:` list. Need another file changed? Report
   `BLOCKED` with the reason — do not improvise cross-cutting edits.
2. **Tests:** run ONLY the test commands your WP lists. **NEVER run the full suite. NEVER run any
   DB-backed test file** (anything using the `pool` fixture) — per-test truncation means two
   concurrent DB runs destroy each other (`tests/conftest.py`). For DB tests you WRITE, self-check
   with `pytest --collect-only <file> -q` and hand execution to the monitor.
3. **No git:** no commits, no branches, no staging. The monitor commits at gates.
4. **Hard invariants** (project `CLAUDE.md`): all writes via `repository.py`; `state_machine.py`,
   `pipelines.py`, `adr_rules.py`, `engine/focus.py` stay pure; everything hermetic on stubs (your
   tests must not need npm, network, or API keys unless the WP says so); new SQL only as the
   numbered migration your WP names.
5. **Security posture** (plan §5): never introduce glob/prefix/regex matching into permission
   logic; never let repo-profile data reach an allow decision; `git clean` never gains `-x`.
6. **Style:** mirror the module you're editing — comment density, naming, docstring voice. This
   codebase explains *why* in docstrings and keeps functions small.
7. **Report format** (final message, exactly this shape):
   ```
   STATUS: DONE | FAILED | BLOCKED
   WP: <id>
   FILES: <paths touched>
   CHECKLIST: <each item: [x] or [ ] + one-line evidence>
   TESTS: <command> -> <pass/fail + tail on fail>
   NOTES: <surprises, decisions, anything the next agent needs>
   ```
   A checklist item you cannot honestly mark `[x]` means STATUS is not DONE. Never claim green
   without pasting the test run's tail.

## 4. Concurrency map (monitor enforces)

- Dispatch by the wave table at the bottom of `WORKFLOW-PROFILE-TASKS.md`. Within a wave, WPs are
  file-disjoint and run as parallel subagents. Across waves, wait.
- One WP = one agent = one attempt. Rescues are fresh agents (with the handoff packet), not
  continuations of the failed session.
- DB test execution is a monitor-only, strictly serial activity at wave ends and gates.
- The monitor keeps a scoreboard (WP → tier → attempts → status) and reports it to the operator
  at every gate.

## 5. QA + delivery protocol (operator-mandated)

1. **Phase gates (A–D):** when a phase's WPs report DONE, the monitor dispatches an **opus QA
   agent**: read the phase diff, the plan sections it implements, and every WP checklist; verify
   claims against the code (not the reports); return findings as
   `BLOCKER | SHOULD-FIX | NIT` with file:line. No code edits in QA mode.
2. **Findings** become fix-WPs: NIT/SHOULD-FIX → haiku; BLOCKER → sonnet (ladder applies). Re-QA
   after fixes.
3. **After opus QA is clean, the monitor runs the end-to-end pass** — opus sign-off alone is
   never delivery:
   - `.venv/bin/python -m orchestrator.cli migrate`
   - FULL `.venv/bin/python -m pytest -q` (serial, sole owner of the DB) → all green
   - Stub E2E smoke: register dev+qa per team → add-goal → `run --max-ticks 50` → all issues done
   - From Gate C on: the approval-flow walkthrough (escalate → `/actions` approve → re-verify)
   - Final gate additionally: `workflow explain --instance tendcharting --team backend` matches
     the live workspace manifest, and WP-24's checklist in full.
4. **Delivery = opus QA clean AND monitor E2E green AND scoreboard shows every WP DONE.** The
   monitor then commits (conventional-commit style, one commit per phase or logical unit) and
   reports to the operator with the scoreboard, test evidence, and anything deferred.

## 6. Monitor (fable) self-restrictions

- Monitor, don't author: no writing implementation code while any lower tier has attempts left.
- Never skip a gate, never overlap DB suites, never let a `blocked_on_approval`-style ambiguity
  pass a gate unexercised.
- If the plan and reality diverge mid-build, amend `WORKFLOW-PROFILE-IMPLEMENTATION-PLAN.md` +
  `WORKFLOW-PROFILE-TASKS.md` first (changelog note), then re-dispatch — agents execute documents,
  not verbal instructions.
- Surface cost/hang anomalies to the operator early (e.g. a wave stuck >30 min, repeated rescues
  on one WP — that's a design smell, see §2 last bullet).
