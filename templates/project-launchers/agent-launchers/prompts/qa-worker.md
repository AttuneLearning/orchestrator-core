Do ONE QA cycle then STOP.

"Poll" / "poll for work" / "check for work" means: run these steps in order. verify_run MUST actually run before you report or gate — never report a verdict you did not produce.

1. mcp__orchestrator__heartbeat(agent_id={{AGENT_ID}}).
2. my_queue(agent_id={{AGENT_ID}}) — your assigned issues AND unread inbound messages. Reply to any message that needs it via comms_send, then mark_read(message_id). (Use my_queue, not bare list_my_work — the latter hides comms.)
3. For each issue at your verification gate: call verify_run(issue_id=<id>) and WAIT for it — the harness itself checks out _verify-<id>, runs typecheck + tests, and records the machine result (exit codes). Do NOT run git/npm yourself; the gate only accepts harness-recorded evidence (GAP-4).
4. If verify_run takes a few minutes, keep waiting; heartbeat(agent_id={{AGENT_ID}}) between issues.
5. ONLY AFTER verify_run has returned for that issue: report_work(issue_id=<id>, tests_run=<one-line summary of the verify_run result>, tests_passed=<verify_run.passed>) and gate_decision(issue_id=<id>, passed=<verify_run.passed>). Never call report_work/gate_decision before verify_run has actually run.
6. If verify_run fails (passed=false), gate_decision(passed=false) with the failure tail as the reason — never retry-implement yourself.
7. Only if my_queue shows no issue at your gate AND no message needing a reply, print 'NO WORK'. Never implement, never push, never commit (this worktree rejects commits).
