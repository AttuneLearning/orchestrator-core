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
- End **every** reply with exactly one line, and exactly one, in this form.
  This line **must be the absolute final line of your reply** — nothing
  after it, not even blank lines or a sign-off. Never quote it, echo it, or
  repeat it anywhere else in your reply (e.g. in a summary or a code block)
  — the side-car scans for the LAST occurrence, so an earlier
  echoed/quoted copy can be mistaken for the real one.
  - `TICK RESULT: WORKED #<id> #<id> ...` — you completed work on these
    orchestrator issue ids this tick. At least one `#<id>` is **mandatory**
    whenever you say WORKED — `TICK RESULT: WORKED` with no ids is a
    protocol violation and will be treated as if no work happened. Append
    `; READY-TO-CLEAR` immediately after (same line) once you've written
    the handoff summary and your context should be reset before the next
    tick.
  - `TICK RESULT: NO WORK (<reason>)` — nothing actionable this tick; give a
    brief reason.
- The ids in `WORKED #<ids>` are orchestrator issue ids, not line numbers or
  file references.
