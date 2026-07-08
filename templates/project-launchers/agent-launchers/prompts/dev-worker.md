Run one {{TEAM}} implementation worker cycle for agent {{AGENT_ID}}.

Use the orchestrator MCP tools as the source of truth: heartbeat, list_my_work, list_issues, claim_issue, adr_list, context_load, report_work, and gate_decision. Work only at gate {{GATE}} for team {{TEAM}} under {{APP}}.

Worker policy: work exactly one issue. Do not spawn subagents, do not claim a batch, and do not coordinate other agents.

If list_my_work is empty, list in-progress issues and claim one unassigned issue whose team is {{TEAM}} and gate_type is {{GATE}}. If there is no work, print 'NO WORK' and stop.

Implement the selected issue on branch issue-<id> from main. Add or adjust tests. If you changed `packages/contracts`, `contracts.seed.json`, or API route files under `apps/api/src`, run `npm run contracts:sync` from the repo root before committing; update `contracts.seed.json` if that command says the seed is stale. Run npm run typecheck and npm test until both pass. Commit only files for the issue, report the sha and branch, then gate_decision passed=true. Never push or merge to main.
