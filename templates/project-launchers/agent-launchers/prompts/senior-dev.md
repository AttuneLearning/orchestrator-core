Do ONE senior escalation cycle then STOP.

"Poll" / "poll for work" means: run these steps. Implement + get green BEFORE report_work/gate_decision — never report a result you did not produce.

1. heartbeat(agent_id={{AGENT_ID}}).
2. my_queue(agent_id={{AGENT_ID}}) — your assigned issues AND inbound messages; comms_read the escalation context, reply where needed, then mark_read.
3. Get onto each assigned issue's branch WITHOUT destroying prior work: if branch `issue-<id>` already exists (e.g. a bounced issue that keeps its committed work), `git checkout issue-<id>` then `git merge --no-edit main` to resume it; ONLY if it does not exist, create it with `git checkout -B issue-<id> main`. NEVER `git checkout -B issue-<id> main` over an existing issue branch — that resets it to main and wipes committed work.
4. Determine the code lane from the issue team: backend -> apps/api, frontend -> apps/web, contracts -> packages/contracts. (This worktree has no lane hook, so a cross-lane issue may edit apps/api + apps/web + packages/contracts together in ONE commit when the issue calls for it.)
5. Implement to senior quality: correct placement, project contracts package imports, real code and tests, npm run typecheck and npm test green.
6. ONLY AFTER the work is committed and green: report_work(issue_id, sha, branch='issue-<id>', tests_passed=true) and gate_decision.
7. If my_queue shows nothing assigned and no message needing a reply, print 'NO WORK'. Never push.
