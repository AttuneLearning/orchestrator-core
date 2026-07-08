Do ONE senior escalation cycle then STOP.

1. heartbeat(agent_id={{AGENT_ID}}).
2. my_queue(agent_id={{AGENT_ID}}) and comms_read the escalation context.
3. For each issue assigned to you, create its branch off current main with git checkout -B issue-<id> main.
4. Determine the code lane from the issue team: backend -> apps/api, frontend -> apps/web, contracts -> packages/contracts.
5. Implement to senior quality: correct placement, project contracts package imports, real code and tests, npm run typecheck and npm test green.
6. Commit only that issue's files on issue-<id>, then report_work(issue_id, sha, branch='issue-<id>', tests_passed=true) and gate_decision.
7. If nothing is assigned to you, print 'NO WORK'. Never push.
