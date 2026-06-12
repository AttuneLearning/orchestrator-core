"""Issue MCP tools — list / get / claim / update_state / gate_decision /
create_subissue / append_log. Thin wrappers over repository.py."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import load_settings
from ..pipelines import load_pipelines
from ..state_machine import apply_gate_decision


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
        pipeline = pipelines[issue.pipeline]
        gate = pipeline.gate(issue.gate_type)
        outcome = apply_gate_decision(
            pipeline, gate, passed=passed, retry_count=issue.retry_count,
            retry_cap=settings.thresholds.retry_cap,
            triggered_by_message=issue.triggered_by_message,
        )
        updated = repo.update_state(
            pool, issue_id, outcome.state, gate_type=outcome.gate_type,
            event_type=outcome.event_type, payload={"reasons": reasons or []},
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
