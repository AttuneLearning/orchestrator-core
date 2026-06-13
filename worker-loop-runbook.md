# Worker Loop Scaffold (pull-worker self-pacing loop)

**Audience:** any pull worker (e.g. agent 4, frontend dev) and its harness.
**Pairs with:** `docs/orchestrator/loop-cadence-spec.md` (the engine/dashboard side).
**Governs:** how a worker polls, works its queue, and self-paces using the cadence the
orchestrator returns from `heartbeat`.

> Source of truth for the *rules* below is the orchestrator (accepted ADRs via `adr_list` /
> `context_load`). This runbook is the *executable shape* of those rules. If they ever
> disagree, the ADR wins — re-read it at the top of each cycle.

---

## Cadence contract

The worker does NOT hardcode poll intervals. It reads them from the orchestrator each poll:

```
heartbeat(agent_id) -> { status, loop_enabled, next_poll_seconds }
```

- `next_poll_seconds` is already resolved server-side (enabled→fast default 300s,
  disabled→slow default 1200s; both operator-customizable on the dashboard `/agents` page).
- The worker simply obeys `next_poll_seconds` for its **idle** wait. It never sleeps while it
  still holds implementation-gate work.

## The loop (pseudocode)

```text
LOOP:
  hb = heartbeat(agent_id)                      # liveness + cadence in one call
  adrs = adr_list(status="accepted")            # honor live protocol every cycle
  q = my_queue(agent_id)                        # MASTER queue: work + inbound messages

  for msg in q.messages:                        # read answers/requests FIRST — they unblock work
    # msg.type is "answer" (a reply to a question this team asked) or "request".
    # msg.issue_id links it to the issue it concerns; msg.reply_to is the original
    # request (full thread); msg.source is who sent it.
    read msg.body (esp. answers whose issue_id matches your assigned/blocked work)
    mark_read(msg.id)                           # consume it so it drops off next poll
    # if it's a request you must action, handle it (e.g. comms_send a response)

  impl = [i for i in q.work if i.gate_type == "implementation"]
  if impl is non-empty:
    for issue in impl:
      context_load(topic=issue.title)           # pre-implementation context
      implement issue + add a Vitest test       # FSD conventions; minimal, in-lane
      commit on working branch (NEVER push)      # capture sha; only the issue's files
      report_work(issue_id, sha, branch, summary, tests_passed=true)
      gate_decision(issue_id, passed=true)       # hands off to QA (verification→e2e→review)
      heartbeat(agent_id)                        # keep alive during long batches
    continue LOOP immediately                    # keep clearing until queue empty

  else:                                          # queue empty
    # QA/verification rejections return as gate_type=implementation in q.work and are
    # picked up by the impl branch above on a later cycle — fixing them IS the loop's job.
    sleep(hb.next_poll_seconds)                  # see "Self-pacing" below
    continue LOOP

# my_queue replaces polling list_my_work alone — it surfaces work AND the answers
# to questions you raised (which list_my_work/comms_check never showed). list_my_work
# remains available as the work-only view.
```

### Halt-vs-continue (per ADR-ORCH-002)

- `loop_enabled = true`  → after the queue empties, keep polling at the fast cadence and pick
  up newly-assigned implementation work. The loop never returns to the human on its own.
- `loop_enabled = false` → after the queue empties, **keep polling at the slow cadence** (it
  does NOT stop). Stays reachable for new assignments within ~one slow interval.
- QA/verification rejections bounce back as implementation-gate work, so the loop fixes them
  automatically on a later cycle, repeating until gates pass. That is the loop's purpose.

## Self-pacing in Claude Code

The "sleep then continue" step is realized by the harness, not a blocking `sleep`:

- **Self-paced `/loop`:** invoke this runbook under `/loop` (no fixed interval) and, at the
  end of each cycle, schedule the next wake with `delaySeconds = hb.next_poll_seconds`.
- **`ScheduleWakeup`:** at the end of a cycle call
  `ScheduleWakeup(delaySeconds = hb.next_poll_seconds, prompt = <re-enter this runbook>)`.
  Re-fetch cadence via `heartbeat` on every wake so operator changes take effect within one
  interval.
- Do not pick a static interval that ignores `next_poll_seconds` — the whole point is that the
  dashboard switch controls cost.

### Token/cache note

`next_poll_seconds = 300` (enabled default) sits at the 5-minute prompt-cache TTL, so idle
polls stay mostly cache-warm and cheap. `1200` (disabled default) busts the cache per wake but
wakes ~4× less often — net cheaper while idle. This is intentional; honor whatever the server
returns.

## Boundaries (unchanged from CLAUDE.md / SoT)

- Own only the **implementation** gate. Never act on verification/e2e/qa_gate/completion.
- Never `push`. Commit only the issue's own files (not `git add -A` over unrelated churn).
- Only work issues assigned to this `agent_id`. Never claim another agent's work.
- Keep changes minimal and conventional; add a test for every implementation.

## Sync note

This runbook describes behavior that should ultimately be **generated from the orchestrator
single source of truth** (see loop-cadence-spec.md §8). Until the render/check tooling exists,
keep it consistent with accepted ADRs by hand; once it exists, this file (and CLAUDE.md /
AGENTS.md) become generated artifacts.
