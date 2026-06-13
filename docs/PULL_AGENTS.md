# Pull agents — integrating live agentic coders

The orchestrator can drive work in two modes, chosen **per gate**:

| Mode | Who does the work | Where |
|---|---|---|
| **`verdict`** (default) | the engine's reasoner / a human / a delegated reviewer renders a decision | over state the orchestrator already holds |
| **`pull`** | a registered **external** worker — a live Claude Code / Codex / Aider — claims the issue and does the work | in the worker's **own repo** |

The orchestrator's job is orchestration (state, gates, ADR governance, off-rails,
routing). It is **not** a coding agent: under `pull` it never edits or executes a
repo. Real agentic coders do that, in repos they were launched in, and report a
**pointer** (commit SHA / PR / test result) back over MCP — the orchestrator never
holds or applies the code.

## The role taxonomy

- **Dev coder** (`function: dev`, `mode: pull`) — writes code *and* tests (TDD).
- **QA runner** (`function: qa`, `mode: pull`) — executes the suite at depth, reports results.
- **Reviewer / lead** (`function: lead`, `mode: verdict`) — consumes the coder's commit
  and the QA results and renders pass/decline (citing ADR violations). Cast by the
  reasoner (default + hermetic), a human, or a delegated reviewer agent.

The line is **edit/execute a repo → pull** vs **render a verdict → push**. A
test-*writing*/test-*running* QA agent is pull; only the verdict is push.

## Install: the `pull-1` pipeline + an external-worker roster

`config/pipelines.yaml` ships `pull-1`:

```
intake(verdict,lead) → implementation(pull,dev) → verification(pull,qa)
                     → qa_gate(verdict,lead) → completion(verdict,lead)
                     → comms_response(verdict,dev, conditional)
```

Copy `config/roster.example.pull.yaml` over `config/roster.yaml` (or merge its
`mode`/`runtime` keys). A `mode: pull` gate **requires** a registered `external`
worker of the gate's `owner` function; with none registered the issue parks
unworked until one appears. `mode: verdict` gates are rendered by the reasoner.

## Register a worker

```bash
python -m orchestrator.cli register-agent --team backend --function dev --runtime external
python -m orchestrator.cli register-agent --team backend --function qa  --runtime external
# the verdict role (reasoner-backed by default):
python -m orchestrator.cli register-agent --team backend --function lead --runtime api
```

## Run a worker (the poll loop)

`examples/pull-agent/loop.py` is a provider-agnostic reference. Each worker is a
long-lived process **started in its own repo**; the orchestrator does not define
or manage that repo.

```bash
python examples/pull-agent/loop.py \
    --agent-id 1 --repo /path/to/api-checkout \
    --coder 'claude -p "{prompt}"'        # or a codex / aider invocation
```

One poll cycle uses these MCP tools (over `python -m orchestrator.cli serve`):

1. `heartbeat(agent_id)` — refresh `last_seen` so liveness reclaim doesn't treat
   the worker as dead.
2. `list_my_work(agent_id)` — issues claimed to this worker, awaiting action.
3. `adr_list(status="accepted")` — the architectural rules to honor in the prompt.
4. run the local coder in `--repo` (edit + test).
5. `report_work(issue_id, sha=..., tests_passed=...)` — store the pointer
   (`code_committed` event). The verdict gate consumes this as evidence.
6. `gate_decision(issue_id, passed=True)` — advance the pull gate.

## Liveness

A pull worker that stops heartbeating for `AGENT_STALE_SECONDS` (default 300) is
**reclaimed**: the engine marks it offline and unassigns the issue, so another
external worker of the same role can pick it up. After `RECLAIM_CAP` (default 3)
reclaims the issue is quarantined (`off_rails`) and its goal paused — exiting only
via a human directive, like any off-rails issue. `claim` sets `last_seen`, so a
freshly-assigned worker gets a full stale-window grace period before its first
heartbeat is due.

## What the orchestrator records

Only pointers, never code: `code_committed` (`{sha, branch, pr_url?, tests_passed?,
summary?}`) and `tests_run` (`{passed, failures, summary}`). These appear in the
issue's `issue_events` timeline and are fed to the verdict reviewer. The code lives
in the worker's repo; promotion (merge / PR) happens there, on the worker's side.
