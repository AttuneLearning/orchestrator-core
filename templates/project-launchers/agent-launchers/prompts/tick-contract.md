# Tick contract (side-car protocol — read every tick)

This is **one tick**, not a session. The side-car injected this prompt because
it is your turn to act *right now*. Obligations:

- Do the work that is available **now**. Never sleep, loop, poll, or wait for
  time/events yourself — you will be woken again by the next injected tick.
- Never invoke runtime slash commands (`/clear`, `/usage`, etc.) — those are
  user-level and the side-car issues them on your signal, not you.
- If there isn't enough left to do, or you are blocked, just say so and yield —
  do not stall waiting for something to change.
- **Between issues**, before moving on or ending the tick: call `memory_write`
  with a short handoff summary of what you just did, so context can be safely
  reset.
- Obey the **CONTEXT BUDGET** line in the tick prompt: it is measured by the
  side-car, not a guess. When it says the context is nearly full, checkpoint
  + `READY-TO-CLEAR`. (If you don't, the side-car force-clears you anyway once
  the budget is exhausted — but a clean, self-chosen clear is better.)
- **The `TICK RESULT` line is advisory — encouraged, not required.** The side-car
  now learns whether you did work from the orchestrator itself: your
  `report_work` / `gate_decision` / commits are the **authoritative** record, and
  the side-car reads them directly. A missing, late, or garbled marker no longer
  strands you as "stuck" or resets your cadence — so never fabricate one, and
  never let formatting it get in the way of doing (and properly reporting) the
  work. But **when you can, still end your reply with exactly one `TICK RESULT`
  line**: it is the fastest, richest *local* signal — it names the exact issue
  ids and carries the `READY-TO-CLEAR` handshake — so the side-car can react this
  tick instead of waiting for the next orchestrator poll. If you emit it, it
  **must be the absolute final line of your reply** (nothing after it, not even a
  blank line or sign-off) and appear exactly once — the side-car scans for the
  LAST occurrence, so an earlier echoed/quoted copy can be mistaken for the real
  one:
  - `TICK RESULT: WORKED #<id> #<id> ...` — you completed work on these
    orchestrator issue ids this tick (ids are orchestrator issue ids, not line
    numbers or files). Give at least one `#<id>` when you say WORKED. Append
    `; READY-TO-CLEAR` (same line) once you've written the handoff summary and
    your context should be reset before the next tick.
  - `TICK RESULT: NO WORK (<reason>)` — nothing actionable this tick; give a
    brief reason.
