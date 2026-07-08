"""Issue MCP tools — list / get / claim / update_state / gate_decision /
create_subissue / append_log. Thin wrappers over repository.py."""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import load_settings
from ..pipelines import load_pipelines
from ..state_machine import apply_gate_decision

_SHA_RE = re.compile(r"^[0-9a-fA-F]{6,40}$")
_STUB_MARKERS = (
    "stub code provider",
    "stub provider output",
    "notimplementederror",
    "placeholder output",
    "placeholder implementation",
)


def _valid_issue_branch(issue_id: int, branch: str) -> bool:
    return branch == f"issue-{issue_id}"


def _looks_like_stub(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(k, ""))
        for k in ("summary", "diff", "content", "provider", "model")
    ).lower()
    return any(marker in text for marker in _STUB_MARKERS)


def _validate_report_work_payload(
    issue_id: int,
    gate_type: str | None,
    sha: str,
    branch: str,
    tests_passed: Optional[bool],
    payload: dict[str, Any],
) -> None:
    if gate_type != "implementation":
        return
    if not _valid_issue_branch(issue_id, branch):
        raise ValueError(
            f"implementation report for issue {issue_id} must use branch "
            f"'issue-{issue_id}'"
        )
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(
            f"implementation report for issue {issue_id} must include a commit sha"
        )
    if tests_passed is not True:
        raise ValueError(
            f"implementation report for issue {issue_id} must set tests_passed=true"
        )
    if _looks_like_stub(payload):
        raise ValueError(
            f"implementation report for issue {issue_id} appears to be stub output"
        )


def _has_valid_implementation_report(pool: ConnectionPool, issue_id: int) -> bool:
    for event in repo.recent_events(pool, issue_id, limit=50):
        if event.event_type == "directive":
            break
        if event.event_type != "code_committed":
            continue
        payload = event.payload or {}
        try:
            _validate_report_work_payload(
                issue_id,
                "implementation",
                str(payload.get("sha", "")),
                str(payload.get("branch", "")),
                payload.get("tests_passed"),
                payload,
            )
        except ValueError:
            continue
        return True
    return False


def register(mcp: FastMCP, pool: ConnectionPool) -> None:
    settings = load_settings()
    pipelines = load_pipelines(settings.pipelines)

    @mcp.tool()
    def list_issues(goal_id: Optional[int] = None,
                    state: Optional[str] = None) -> list[dict[str, Any]]:
        """List issues, optionally filtered by goal_id and/or state."""
        states = [state] if state else None
        return [asdict(i) for i in repo.list_issues(pool, goal_id=goal_id, states=states)]

    @mcp.tool()
    def get_issue(issue_id: int) -> Optional[dict[str, Any]]:
        """Fetch a single issue by id."""
        issue = repo.get_issue(pool, issue_id)
        return asdict(issue) if issue else None

    @mcp.tool()
    def claim_issue(issue_id: int, agent_id: int) -> dict[str, Any]:
        """Assign an issue to an agent (marks the agent busy)."""
        repo.claim_issue(pool, issue_id, agent_id)
        return asdict(repo.get_issue(pool, issue_id))

    @mcp.tool()
    def update_state(issue_id: int, to_state: str,
                     gate_type: Optional[str] = None) -> dict[str, Any]:
        """Transition an issue to a new state (writes a matching event)."""
        return asdict(repo.update_state(pool, issue_id, to_state, gate_type=gate_type))

    @mcp.tool()
    def gate_decision(issue_id: int, passed: bool, reasons: Optional[list[str]] = None) -> dict[str, Any]:
        """Record a gate_review decision and advance the issue accordingly."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        if passed and issue.gate_type == "implementation":
            if not _has_valid_implementation_report(pool, issue_id):
                raise ValueError(
                    f"issue {issue_id} cannot pass implementation without a valid "
                    f"code_committed report on branch 'issue-{issue_id}'"
                )
        pipeline = pipelines[issue.pipeline]
        gate = pipeline.gate(issue.gate_type)
        outcome = apply_gate_decision(
            pipeline, gate, passed=passed, retry_count=issue.retry_count,
            retry_cap=settings.thresholds.retry_cap,
            triggered_by_message=issue.triggered_by_message,
        )
        payload = {"reasons": reasons or []}
        if issue.gate_type == "e2e" and passed:
            from ..backup import record_backup
            payload["database_backup"] = record_backup(
                pool, settings,
                reason=f"after-e2e-issue-{issue.id}",
                issue_id=issue.id,
                goal_id=issue.goal_id,
            )
        updated = repo.update_state(
            pool, issue_id, outcome.state, gate_type=outcome.gate_type,
            event_type=outcome.event_type, payload=payload,
            retry_count=outcome.retry_count,
        )
        return asdict(updated)

    @mcp.tool()
    def create_subissue(parent_id: int, title: str, description: str = "") -> dict[str, Any]:
        """Create a child issue under a parent (inherits goal, depth+1)."""
        parent = repo.get_issue(pool, parent_id)
        if parent is None:
            raise ValueError(f"no parent issue {parent_id}")
        return asdict(repo.create_subissue(pool, parent, title, description))

    @mcp.tool()
    def apply_directive(issue_id: int, note: str = "",
                        actor: str = "human") -> dict[str, Any]:
        """Un-quarantine an off_rails issue (human directive; resets counters)."""
        return asdict(repo.apply_directive(pool, issue_id, "resume",
                                           note=note, actor=actor))

    @mcp.tool()
    def append_log(issue_id: int, event_type: str,
                   payload: Optional[dict[str, Any]] = None) -> dict[str, str]:
        """Append a non-transition event to an issue's log."""
        repo.append_log(pool, issue_id, event_type, payload or {})
        return {"status": "ok"}

    @mcp.tool()
    def heartbeat(agent_id: int) -> dict[str, Any]:
        """Pull workers call this every poll: refreshes last_seen (so the engine's
        liveness reclaim doesn't treat them as dead), reactivates a reclaimed
        worker (offline -> idle), and returns the live loop policy so the worker
        self-paces. `next_poll_seconds` is 0 when looping is disabled, meaning
        'stop after the queue drains'; otherwise it's the idle poll cadence."""
        repo.touch_agent(pool, agent_id)
        agent = repo.get_agent(pool, agent_id)
        if agent is None:
            # No such agent in THIS coordinator's DB — almost always means the MCP
            # server is pointed at the wrong database. Signal it clearly instead of
            # silently reporting loop_enabled=False (which reads as "looping disabled").
            return {
                "status": "unknown_agent",
                "agent_id": agent_id,
                "note": ("no agent %d in this coordinator — the MCP server is likely "
                         "connected to the wrong database. Launch it with "
                         "`--instance <project>` (e.g. tendcharting)." % agent_id),
                "loop_enabled": False,
                "next_poll_seconds": 0,
            }
        reactivated = False
        if agent is not None and agent.status == "offline":
            repo.set_agent_status(pool, agent_id, "idle")
            reactivated = True
        loop_enabled = bool(agent.loop_enabled) if agent else False
        # Cooldown window (migration 0019): if paused_until is in the future, the
        # worker should sleep until then instead of polling; the engine also skips
        # assigning to it. pause_seconds is how long to sleep (0 = active).
        import datetime as _dt
        paused_until = agent.paused_until if agent else None
        pause_seconds = 0
        if paused_until is not None:
            pause_seconds = max(0, int((paused_until - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))
        return {
            "status": "ok",
            "reactivated": reactivated,
            "loop_enabled": loop_enabled,
            "next_poll_seconds": repo.agent_next_poll_seconds(agent) if agent else 0,
            "paused_until": paused_until.isoformat() if paused_until else None,
            "pause_seconds": pause_seconds,
        }

    @mcp.tool()
    def list_my_work(agent_id: int) -> list[dict[str, Any]]:
        """Issues currently assigned to this pull worker and awaiting its action
        (in_progress). One call for a poll loop to find what to do next. For the
        full picture (work + inbound messages) use my_queue."""
        return [
            asdict(i) for i in repo.list_issues(
                pool, states=["in_progress"], assigned_agent=agent_id)
        ]

    def _msg_item(m: dict[str, Any], kind_label: str) -> dict[str, Any]:
        # Every message carries its originating issue + source + thread link so
        # history is always discoverable from the queue.
        return {
            "id": m["id"], "type": kind_label,
            "subject": m["subject"], "body": m["body"],
            "source": m["from_team"], "issue_id": m.get("issue_id"),
            "reply_to": m.get("reply_to"), "priority": m.get("priority"),
            "read_at": m.get("read_at"), "created_at": m.get("created_at"),
        }

    @mcp.tool()
    def my_queue(agent_id: int) -> dict[str, Any]:
        """The master queue: everything on this agent's plate in one call.
        `work` = its assigned in-progress issues (same as list_my_work). `messages`
        = UNREAD inbound for its team — answers to questions it asked (type
        'answer') and any pending requests (type 'request'), each linked to its
        originating issue (issue_id), source (from_team) and thread (reply_to).
        Call mark_read(message_id) after consuming a message so it drops off."""
        agent = repo.get_agent(pool, agent_id)
        work = [asdict(i) for i in repo.list_issues(
            pool, states=["in_progress"], assigned_agent=agent_id)]
        messages: list[dict[str, Any]] = []
        if agent is not None:
            messages += [_msg_item(m, "answer")
                         for m in repo.list_responses(pool, to_team=agent.team,
                                                      unread_only=True)]
            messages += [_msg_item(m, "request")
                         for m in repo.pending_messages(pool, to_team=agent.team)]
        return {"work": work, "messages": messages}

    @mcp.tool()
    def mark_read(message_id: int) -> dict[str, str]:
        """Mark an inbox message consumed so it drops out of the next my_queue."""
        repo.mark_message_read(pool, message_id)
        return {"status": "ok"}

    @mcp.tool()
    def report_work(issue_id: int, sha: str = "", branch: str = "",
                    pr_url: str = "", tests_passed: Optional[bool] = None,
                    summary: str = "") -> dict[str, str]:
        """Record the code a pull worker produced (a `code_committed` event).
        Reports a pointer to the work in the worker's OWN repo — the orchestrator
        never holds or applies the code. The verdict gate consumes this as
        evidence. Call before gate_decision to advance the gate."""
        payload: dict[str, Any] = {"sha": sha}
        if branch:
            payload["branch"] = branch
        if pr_url:
            payload["pr_url"] = pr_url
        if tests_passed is not None:
            payload["tests_passed"] = tests_passed
        if summary:
            payload["summary"] = summary
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        _validate_report_work_payload(
            issue_id, issue.gate_type, sha, branch, tests_passed, payload,
        )
        repo.append_log(pool, issue_id, "code_committed", payload)
        return {"status": "ok"}
