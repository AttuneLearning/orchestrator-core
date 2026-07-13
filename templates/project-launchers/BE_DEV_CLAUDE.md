# Backend-Dev Model Tiering

Instructions for the `backend_dev` pull worker (agent_id 1) on how to route work
across Claude model tiers. The orch-manager integrates this into Claude settings.

## Core policy

1. **Plan with Opus.** Use Opus to plan **only the current work** — the issue in
   hand, nothing speculative. Opus also owns the non-delegable judgment steps:
   contract definition (ADR-DEV-001 — contracts-first; keep `contracts.seed.json`
   in sync via `npm run contracts:sync`; never invent or paper over a shape),
   ADR compliance, git, and the orchestrator calls (`report_work`,
   `gate_decision`). The plan must be written so a Haiku worker can execute it end
   to end with no design decisions left open.
2. **Implement with Haiku.** Hand the Opus plan to Haiku and let Haiku complete
   the mechanical work (edit code, add tests, typecheck, run tests). Opus reviews
   the result and performs the commit + orchestrator reporting.
3. **Escalate Haiku → Sonnet after 2 stalls.** If Haiku gets stuck **more than
   twice** on the same work, elevate that work to Sonnet.
4. **Escalate Sonnet → Opus after 2 stalls.** If Sonnet then gets stuck **twice**,
   elevate the work back to Opus.
5. **Opus completes only as last resort.** Only after the Haiku → Sonnet → Opus
   escalation chain is exhausted does Opus implement the code itself.

## Single vs. multiple agents

- **Fan out only when the work genuinely parallelizes** — independent
  files/concerns that don't share state. Then run several Haiku agents together.
- **Stay singular** for a single-file/one-concern change (the ADR-PROC-001 issue
  size). Adding a subagent round-trip for a trivial edit costs more than it saves;
  skip delegation entirely for true one-liners and let Opus do it inline.

## What counts as "stuck"

A stall is any one of: a failing typecheck/test cycle the worker cannot green, a
repeated wrong-lane or out-of-scope commit, an inability to satisfy the issue's
governing ADRs, or a verify-gate rejection. Count stalls **per unit of work**
(one issue), and reset the counter when the work moves to a new tier.

## Escalation ladder (summary)

| Stage | Model  | Responsibility                                   |
|-------|--------|--------------------------------------------------|
| Plan  | Opus   | Plan the current issue + verify contracts/ADRs   |
| Build | Haiku  | Implement + test; escalate after 2+ stalls       |
| Retry | Sonnet | Take over stuck work; escalate after 2 stalls    |
| Final | Opus   | Implement directly only after Sonnet is exhausted|

## Notes

- Plan the **current** work only — do not pre-plan future issues.
- Escalation is per-issue; each tier gets a fresh stall counter.
- Contract definition, commit, and gate decisions stay with Opus regardless of
  which tier wrote the code — a cheaper tier must not self-approve its own gate.
- All other backend-dev boundaries (write scope = `apps/api/`,
  `packages/contracts/`, `contracts.seed.json`; run `npm run contracts:sync` when
  the API surface changes; one-issue-per-branch; never push/merge; honor per-issue
  ADRs) remain in force regardless of tier.
