"""Domain models: enums and dataclasses mirroring the Postgres schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class IssueState(str, Enum):
    """Issue lifecycle states (see plan: state machine).

    backlog → planning → ready → in_progress → in_review → done
    with blocked / failed / off_rails as side states.
    """

    BACKLOG = "backlog"
    PLANNING = "planning"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    OFF_RAILS = "off_rails"


class GoalState(str, Enum):
    BACKLOG = "backlog"
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"


class GateType(str, Enum):
    """The five gates of pipeline #1 (PROCESS_GUIDE.md)."""

    INTAKE = "intake"
    IMPLEMENTATION = "implementation"
    QA_GATE = "qa_gate"
    COMPLETION = "completion"
    COMMS_RESPONSE = "comms_response"


class EventType(str, Enum):
    """Kinds of rows in the append-only issue_events log."""

    CREATED = "created"
    STATE_CHANGE = "state_change"
    GATE_ENTER = "gate_enter"
    GATE_PASS = "gate_pass"
    GATE_DECLINE = "gate_decline"
    CODE_GENERATED = "code_generated"
    ERROR = "error"
    DRIFT_SCORE = "drift_score"
    REENGAGED = "reengaged"
    CONTEXT_SNAPSHOT = "context_snapshot"


@dataclass
class Goal:
    id: int
    title: str
    description: str = ""
    state: str = GoalState.BACKLOG.value
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Issue:
    id: int
    goal_id: int
    title: str
    description: str = ""
    parent_id: Optional[int] = None
    depth: int = 0
    team: str = "backend"
    pipeline: str = "pipeline-1"
    state: str = IssueState.BACKLOG.value
    gate_type: Optional[str] = None
    retry_count: int = 0
    step_count: int = 0
    assigned_agent: Optional[int] = None
    triggered_by_message: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Agent:
    id: int
    team: str
    function: str = "dev"
    runtime: str = "api"
    status: str = "idle"
    created_at: Optional[datetime] = None


@dataclass
class IssueEvent:
    id: int
    issue_id: int
    seq: int
    event_type: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass
class MemoryNote:
    id: int
    scope: str
    body: str
    created_at: Optional[datetime] = None
