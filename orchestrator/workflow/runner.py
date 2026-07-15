"""Step runner: executes one workflow step's actions in order.

For each action in `profile.step(step_name).actions_for(role)`, in order:
  1. Authorize (permissions.authorize). `deny` fails the step immediately, naming
     the action. `escalate` short-circuits the step as `blocked_on_approval` — no
     later actions run. The decision is delegated to an optional
     `escalation_cb(action, phase) -> str` hook, where `phase` is "authorize"
     for this case (the action has never run and needs first-run approval).
     `escalation_cb=None` (the default) always means "pending" (blocked). A
     callback may return "approved" to let the action execute anyway (WP-12
     wires a real one — `escalation.make_escalation_cb` — backed by
     `pending_actions` + comms), or "denied" (WP-13) — a hard stop, distinct
     from "pending": the step fails NOW (`StepResult.status == "failed"`),
     naming the denial in `reason`, and does NOT re-escalate.
  2. Skip check: when the action names both `when_changed` globs and a `sentinel`,
     and the sentinel is not stale (see `sentinel.is_stale`), the action is
     skipped (`skipped="unchanged"`) and the step moves on.
  3. Execute: a `builtin` action resolves through the stack adapter's
     `builtins()` map (`profile.stack` selects the adapter); a `run` action goes
     through `subprocess.run(shell=True, cwd=worktree, ...)`. Timeouts and
     unexpected exceptions from a builtin become a failed ActionResult — they
     never escape `run_step`.
  4. Failure handling honors `action.on_fail`: `block` fails the step now;
     `warn` records the failure and continues to the next action; `escalate`
     goes through the same `escalation_cb` hook as authorize-time escalation,
     but with `phase="on_fail"` (the action DID run and failed; approving here
     means retry, a distinct case from phase="authorize" — see plan Gate A QA
     finding 4b and `orchestrator/workflow/escalation.py`). A "denied" decision
     here also fails the step now, same as the authorize-time case.
  5. On success, if the action carries a sentinel, `write_sentinel` records the
     digest computed in step 2 — sentinels are only ever written after success.

`event_cb(kind, payload)` fires exactly once per action outcome, where kind is
one of: executed | refused | escalated | skipped | failed. Payloads are plain
JSON-safe dicts. This module imports NOTHING from `orchestrator.repository` or
any DB layer — the caller (verify_run, _apply_in_worktree, WP-08/09) wires
`event_cb` to `repository.append_log` and `escalation_cb` to the real
escalation glue (WP-12's `escalation.make_escalation_cb`).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sentinel as sentinel_mod
from .adapters import get_adapter
from .models import Profile, RequiredAction
from .permissions import Permissions, authorize

EventCallback = Callable[[str, dict[str, Any]], None]
EscalationCallback = Callable[[RequiredAction, str], str]


@dataclass
class ActionResult:
    """Outcome of a single action within a step."""

    action: RequiredAction
    """The action this result describes."""

    verdict: str
    """Authorization verdict: allow | deny | escalate."""

    ok: bool
    """True iff the action succeeded (or was skipped as unchanged)."""

    skipped: str = ""
    """Non-empty (e.g. "unchanged") when the action was not executed at all."""

    detail: dict[str, Any] = field(default_factory=dict)
    """Execution detail (returncode/stdout/stderr/reason, or a builtin's own dict)."""


@dataclass
class StepResult:
    """Outcome of running a whole step."""

    status: str
    """"ok" | "failed" | "blocked_on_approval"."""

    results: list[ActionResult] = field(default_factory=list)
    """Per-action results, in execution order, up to the point of short-circuit."""

    reason: str = ""
    """Human-readable reason when status != "ok" (names the offending action)."""


def _action_label(action: RequiredAction) -> str:
    """Short human-readable identity for an action, for reasons/logging."""
    return action.run.strip() or action.builtin.strip() or "<unnamed action>"


def _action_json(action: RequiredAction) -> dict[str, Any]:
    """JSON-safe projection of an action for event payloads."""
    return {
        "run": action.run,
        "builtin": action.builtin,
        "on_fail": action.on_fail,
        "timeout": action.timeout,
        "source": action.source,
    }


def _payload(action: RequiredAction, verdict: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": _action_json(action), "verdict": verdict}
    payload.update(extra)
    return payload


def _emit(event_cb: EventCallback | None, kind: str, payload: dict[str, Any]) -> None:
    if event_cb is not None:
        event_cb(kind, payload)


def _resolve_escalation(
    action: RequiredAction, escalation_cb: EscalationCallback | None, phase: str
) -> str:
    """Ask whether an escalated action may proceed.

    `phase` tells the callback WHY this is escalating — "authorize" (the
    action never ran; this is a first-run permission gate) or "on_fail" (the
    action ran and failed; approving here means retry). The two are
    indistinguishable to an approver unless this is threaded through (Gate A
    QA finding 4b), so the runner always names which case it is.

    `escalation_cb=None` is the WP-07 default: always "pending" (i.e.
    blocked), regardless of phase. A caller-supplied callback (WP-12's
    `escalation.make_escalation_cb`) may answer "approved" instead, in which
    case the action executes as if allowed, or "denied" (WP-13) — a hard stop
    that fails the step now instead of blocking it; the caller (`run_step`)
    maps that outcome, not this helper.
    """
    if escalation_cb is None:
        return "pending"
    return escalation_cb(action, phase)


def _run_builtin(worktree: str | Path, profile: Profile, action: RequiredAction) -> dict[str, Any]:
    """Execute a builtin action via the profile's stack adapter.

    Returns a JSON-safe dict with at least `ok`/`reason`. Never raises — an
    unknown stack, unknown builtin name, or an exception from the builtin
    itself all become a `{"ok": False, "reason": ...}` result.
    """
    adapter = get_adapter(profile.stack)
    if adapter is None:
        return {"ok": False, "reason": f"no adapter for stack {profile.stack!r}"}

    fn = adapter.builtins().get(action.builtin)
    if fn is None:
        return {"ok": False, "reason": f"unknown builtin: {action.builtin!r}"}

    try:
        result = fn(worktree)
    except Exception as exc:  # builtins are adapter code; never let them crash the runner
        return {"ok": False, "reason": f"builtin {action.builtin!r} raised: {exc}"}

    detail = dict(result)
    detail.setdefault("ok", False)
    detail.setdefault("reason", "")
    return detail


def _run_shell(worktree: str | Path, action: RequiredAction) -> dict[str, Any]:
    """Execute a `run` action's shell command. Timeouts never raise past here."""
    try:
        proc = subprocess.run(
            action.run,
            shell=True,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=action.timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": f"timed out after {action.timeout}s",
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }

    ok = proc.returncode == 0
    return {
        "ok": ok,
        "reason": "" if ok else f"exit code {proc.returncode}",
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _execute(worktree: str | Path, profile: Profile, action: RequiredAction) -> dict[str, Any]:
    if action.builtin:
        return _run_builtin(worktree, profile, action)
    return _run_shell(worktree, action)


def run_step(
    worktree: str | Path,
    profile: Profile,
    step_name: str,
    role: str | None,
    perms: Permissions,
    *,
    event_cb: EventCallback | None = None,
    escalation_cb: EscalationCallback | None = None,
) -> StepResult:
    """Run every action of `step_name` (role-resolved) in order.

    Args:
        worktree: checkout root the actions execute against.
        profile: the effective Profile (provides stack for builtin resolution).
        step_name: which step to run (e.g. "prepare", "verify", "cleanup").
        role: caller's role, used to pick role-specific actions when present.
        perms: workspace-granted Permissions used to authorize each action.
        event_cb: optional (kind, payload) -> None sink, called once per action
            outcome. kind is one of executed|refused|escalated|skipped|failed.
        escalation_cb: optional (action) -> "approved"|"pending" hook for
            authorize-time and on_fail="escalate" escalations. Defaults to
            always "pending" (no persistence in this WP; see WP-12).

    Returns:
        A StepResult with status "ok" | "failed" | "blocked_on_approval".
    """
    step = profile.step(step_name)
    results: list[ActionResult] = []

    for action in step.actions_for(role):
        verdict = authorize(action, perms)

        if verdict == "deny":
            reason = f"denied: {_action_label(action)}"
            results.append(ActionResult(action=action, verdict=verdict, ok=False, detail={"reason": reason}))
            _emit(event_cb, "refused", _payload(action, verdict, reason=reason))
            return StepResult(status="failed", results=results, reason=reason)

        if verdict == "escalate":
            decision = _resolve_escalation(action, escalation_cb, "authorize")
            if decision == "denied":
                reason = f"denied: {_action_label(action)}"
                results.append(
                    ActionResult(action=action, verdict=verdict, ok=False, detail={"decision": decision})
                )
                _emit(event_cb, "escalated", _payload(action, verdict, decision=decision, reason=reason))
                return StepResult(status="failed", results=results, reason=reason)
            if decision != "approved":
                reason = f"awaiting approval: {_action_label(action)}"
                results.append(
                    ActionResult(action=action, verdict=verdict, ok=False, detail={"decision": decision})
                )
                _emit(event_cb, "escalated", _payload(action, verdict, decision=decision))
                return StepResult(status="blocked_on_approval", results=results, reason=reason)
            verdict = "allow"  # approved: proceed as if authorized

        # Skip check: only meaningful when both when_changed globs and a
        # sentinel name are set (the sentinel needs both to know what to hash
        # and where to compare against).
        digest: str | None = None
        if action.when_changed and action.sentinel:
            stale, digest = sentinel_mod.is_stale(worktree, action.when_changed, action.sentinel)
            if not stale:
                results.append(ActionResult(action=action, verdict=verdict, ok=True, skipped="unchanged"))
                _emit(event_cb, "skipped", _payload(action, verdict, skipped="unchanged"))
                continue

        detail = _execute(worktree, profile, action)
        ok = bool(detail.get("ok", False))

        if ok:
            results.append(ActionResult(action=action, verdict=verdict, ok=True, detail=detail))
            _emit(event_cb, "executed", _payload(action, verdict, **detail))
            if action.sentinel and digest is not None:
                sentinel_mod.write_sentinel(worktree, action.sentinel, digest)
            continue

        # Failure path: on_fail decides what happens next.
        results.append(ActionResult(action=action, verdict=verdict, ok=False, detail=detail))
        _emit(event_cb, "failed", _payload(action, verdict, **detail))

        if action.on_fail == "warn":
            continue

        if action.on_fail == "escalate":
            decision = _resolve_escalation(action, escalation_cb, "on_fail")
            if decision == "approved":
                continue
            if decision == "denied":
                reason = f"denied (retry after failure): {_action_label(action)}"
                _emit(event_cb, "escalated", _payload(action, verdict, decision=decision, reason=reason))
                return StepResult(status="failed", results=results, reason=reason)
            reason = f"awaiting approval after failure: {_action_label(action)}"
            _emit(event_cb, "escalated", _payload(action, verdict, decision=decision))
            return StepResult(status="blocked_on_approval", results=results, reason=reason)

        # on_fail == "block" (also the fallback for any unrecognized value)
        reason = f"failed: {_action_label(action)}: {detail.get('reason', '')}"
        return StepResult(status="failed", results=results, reason=reason)

    return StepResult(status="ok", results=results)
