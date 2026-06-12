"""Issue state machine. Pure — operates on plain values, never touches the DB.

Lifecycle:

    backlog → planning → ready → in_progress → in_review[gate] → done
                                      ↑________________| (decline → retry_count++)
       blocked (deps/sub-issues)   failed (retry cap)   off_rails (quarantine, latched)

The engine calls validate_transition() before persisting any state change, and
apply_gate_decision() to compute the result of a gate_review. off_rails is a
latched terminal-ish quarantine state (only a human directive leaves it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import IssueState
from .pipelines import Gate, Pipeline, next_gate

# Legal state transitions. off_rails is reachable from any active state (latch).
_ACTIVE = {
    IssueState.BACKLOG.value,
    IssueState.PLANNING.value,
    IssueState.READY.value,
    IssueState.IN_PROGRESS.value,
    IssueState.IN_REVIEW.value,
    IssueState.BLOCKED.value,
}

_ALLOWED: dict[str, set[str]] = {
    IssueState.BACKLOG.value: {IssueState.PLANNING.value, IssueState.READY.value,
                               IssueState.BLOCKED.value},
    IssueState.PLANNING.value: {IssueState.READY.value, IssueState.BLOCKED.value},
    IssueState.READY.value: {IssueState.IN_PROGRESS.value, IssueState.BLOCKED.value},
    IssueState.IN_PROGRESS.value: {IssueState.IN_REVIEW.value, IssueState.BLOCKED.value,
                                   IssueState.FAILED.value},
    IssueState.IN_REVIEW.value: {IssueState.IN_PROGRESS.value, IssueState.DONE.value,
                                 IssueState.FAILED.value},
    IssueState.BLOCKED.value: {IssueState.READY.value, IssueState.IN_PROGRESS.value},
    # terminal / latched
    IssueState.DONE.value: set(),
    IssueState.FAILED.value: set(),
    IssueState.OFF_RAILS.value: set(),
}


def validate_transition(from_state: str, to_state: str) -> bool:
    """True if from_state → to_state is legal. off_rails is always reachable from active."""
    if to_state == IssueState.OFF_RAILS.value:
        return from_state in _ACTIVE
    return to_state in _ALLOWED.get(from_state, set())


@dataclass(frozen=True)
class GateOutcome:
    state: str
    gate_type: Optional[str]
    retry_count: int
    event_type: str  # gate_pass | gate_decline


def apply_gate_decision(
    pipeline: Pipeline,
    current_gate: Gate,
    *,
    passed: bool,
    retry_count: int,
    retry_cap: int,
    triggered_by_message: bool = False,
) -> GateOutcome:
    """Compute the next (state, gate, retry_count) from a gate_review result.

    Pass  → advance to the next applicable gate, or DONE if this was the last.
    Decline → increment retry_count; route back to the gate's on_failure target
              (or redo the current gate). At/over the retry cap → FAILED.
    """
    if passed:
        nxt = next_gate(pipeline, current_gate.type,
                        triggered_by_message=triggered_by_message)
        if nxt is None:
            return GateOutcome(IssueState.DONE.value, None, retry_count, "gate_pass")
        return GateOutcome(IssueState.IN_PROGRESS.value, nxt.type, retry_count, "gate_pass")

    retry_count += 1
    if retry_count >= retry_cap:
        return GateOutcome(IssueState.FAILED.value, current_gate.type, retry_count,
                           "gate_decline")
    target = current_gate.on_failure or current_gate.type
    return GateOutcome(IssueState.IN_PROGRESS.value, target, retry_count, "gate_decline")
