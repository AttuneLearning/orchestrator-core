"""Apply/verify leg (slice F). OFF by default (settings.apply_enabled).

Changes the stored-only posture deliberately and narrowly:
  - artifacts are applied only in a disposable git worktree, never the primary
    checkout;
  - the verify command runs in that worktree with a timeout;
  - results land in issue_events as `verification` events for the qa_gate
    reviewer to consume;
  - promotion (merging the worktree branch) happens only via the explicit human
    CLI command `apply-promote` — the engine never merges, and nothing pushes.
"""
