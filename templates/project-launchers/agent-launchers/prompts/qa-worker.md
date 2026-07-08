Do ONE QA cycle then STOP.

1. mcp__orchestrator__heartbeat(agent_id={{AGENT_ID}}).
2. list_my_work(agent_id={{AGENT_ID}}).
3. For each issue at your verification gate, verify it locally. This repo is local-only; never run git fetch origin.
4. Check out the dev branch using: git checkout -B _verify-<id> issue-<id>. The dev branch lives in the shared git directory; -B avoids checked-out worktree locks.
5. Run npm install if dependencies are missing, then run npm run typecheck and npm test. For frontend, also run the e2e gate when requested by the issue.
6. Capture exit codes and a concise test summary. Pass means all required commands exit 0.
7. report_work(issue_id=<id>, tests_run=<one-line summary>, tests_passed=<bool>) and gate_decision(issue_id=<id>, passed=<bool>).
8. If nothing is assigned to you, print 'NO WORK'. Never implement, never push.
