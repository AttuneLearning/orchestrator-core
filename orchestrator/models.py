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
    CANCELLED = "cancelled"  # terminal; operator/auto triage (no further work)


class GoalState(str, Enum):
    SUGGESTED = "suggested"  # externally proposed; inert until a human promotes
    BACKLOG = "backlog"
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    REJECTED = "rejected"  # a suggestion a human declined


class GateType(str, Enum):
    """The gates of the issue lifecycle (PROCESS_GUIDE.md).

    The first five are pipeline #1; `verification` is the pull-model gate where a
    QA runner executes the suite (config/pipelines.yaml `pull-1`).
    """

    INTAKE = "intake"
    CONTRACT_CHECK = "contract_check"  # contract-first triage (config/pipelines.yaml pull-fe)
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    E2E = "e2e"
    QA_GATE = "qa_gate"
    COMPLETION = "completion"
    COMMS_RESPONSE = "comms_response"


class ContractStatus(str, Enum):
    """API contract lifecycle (migration 0011). A contract is *satisfied* (a
    consumer may build against it) once it is agreed or live."""

    PROPOSED = "proposed"      # shape requested; inert until the owner agrees
    AGREED = "agreed"          # owner agreed the shape; consumers may build now
    LIVE = "live"              # endpoint actually implemented
    DEPRECATED = "deprecated"  # superseded; historical only


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
    PLAN = "plan"
    DIRECTIVE = "directive"
    COMMS_RESPONSE = "comms_response"
    DECOMPOSED = "decomposed"
    VERIFICATION = "verification"
    PROMOTED = "promoted"
    ADR_PROPOSED = "adr_proposed"
    # Pull-model evidence reported by external workers via MCP. code_committed:
    # {sha, branch, pr_url?, tests_passed?, diff?}; tests_run: {passed, failures,
    # summary}. The verdict (gate_review) consumes these instead of running code.
    CODE_COMMITTED = "code_committed"
    TESTS_RUN = "tests_run"
    CANCELLED = "cancelled"      # issue cancelled (operator/auto triage)
    ALERT = "alert"              # decomposition cap / routing-invariant breach
    CONTRACT_BLOCKED = "contract_blocked"  # contract_check held the issue on missing contracts


@dataclass
class Goal:
    id: int
    title: str
    description: str = ""
    state: str = GoalState.BACKLOG.value
    pipeline: str = "pipeline-1"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    suggested_by: str = ""  # provenance for externally proposed goals
    source: str = ""        # free-text rationale / origin of a suggestion
    # Decomposition override (migration 0010): None = simple-goal heuristic;
    # 'single' = exactly one implementation issue; 'full' = force decomposition.
    decompose: Optional[str] = None
    # Goal kind (migration 0017): 'standard' | 'maintenance'. A maintenance goal is
    # a perpetual standing backlog whose issues backfill idle team capacity (worked
    # only when the team has no standard work) and which never auto-completes.
    kind: str = "standard"


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
    origin_message_id: Optional[int] = None
    work_type: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Agent:
    id: int
    team: str
    function: str = "dev"      # dev | qa | lead (lead = reviewer / verdict)
    runtime: str = "api"       # api | cli | external (external = pull daemon)
    status: str = "idle"
    last_seen: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # Pull-loop policy (migration 0009). loop_enabled=false -> worker stops after
    # draining its queue; true -> keeps polling every poll_interval_seconds.
    loop_enabled: bool = False
    poll_interval_seconds: int = 300
    # Cooldown / auto-retry window (migration 0019). When set to a future time the
    # engine won't assign new work and a pull worker sleeps until then, then resumes.
    # Set on a token-limit backoff (now()+2h) or manually from the dashboard.
    paused_until: Optional[datetime] = None


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


@dataclass
class Contract:
    """One API endpoint contract, keyed (method, path). The DB row is the
    coordination layer the engine gates on; request_ref/response_dto/source_ref
    are pointers into the owning repo's authoritative schema (migration 0011)."""

    id: int
    method: str
    path: str
    request_ref: str = ""
    response_dto: str = ""
    auth: str = "none"
    owner_team: str = "backend"
    status: str = ContractStatus.PROPOSED.value
    version: str = "1.0"
    content_hash: Optional[str] = None
    source_ref: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
