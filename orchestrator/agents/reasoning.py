"""Reasoning agent.

Uses the Anthropic SDK for structured decisions when ANTHROPIC_API_KEY is set;
otherwise falls back to a deterministic StubReasoner so the engine runs and is
testable in-session without a key or network. Both implement the same four
operations the engine depends on:

    decompose_goal(goal)        -> list[IssueSpec]
    plan_issue(issue)           -> str
    gate_review(issue, gate)    -> GateReview
    score_drift(issue, events)  -> float   (1.0 = perfectly on-track, 0.0 = adrift)
"""

from __future__ import annotations

import json
from typing import Any, Optional, Protocol

from ..config import Settings
from ..models import Goal, Issue
from .base import GateReview, IssueSpec, TriageDecision, extract_json


class Reasoner(Protocol):
    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]: ...
    def plan_issue(self, issue: Issue) -> str: ...
    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list[dict[str, Any]]] = None) -> GateReview: ...
    def score_drift(self, issue: Issue,
                    recent: Optional[list[dict[str, Any]]] = None) -> float: ...
    def triage_message(self, message: dict[str, Any]) -> TriageDecision: ...


class StubReasoner:
    """Deterministic, network-free reasoner for hermetic runs and tests."""

    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]:
        specs = [
            IssueSpec(title=f"Implement: {goal.title}", description=goal.description),
            IssueSpec(title=f"Test: {goal.title}", description="Add tests and verify."),
        ]
        return specs[:max_subissues]

    def plan_issue(self, issue: Issue) -> str:
        return f"Plan for '{issue.title}': implement, add tests, verify, complete."

    def gate_review(self, issue, gate_type, recent=None) -> GateReview:
        # Stub passes every gate so issues flow to done in the happy path.
        return GateReview(passed=True, reasons=[f"{gate_type} exit criteria met (stub)"])

    def score_drift(self, issue, recent=None) -> float:
        return 1.0

    def triage_message(self, message: dict[str, Any]) -> TriageDecision:
        # Stub accepts every inbound request; title derives from the subject.
        return TriageDecision(accept=True, title=message["subject"],
                              description=message.get("body", ""))


class AnthropicReasoner:
    """Structured decisions via the Anthropic SDK."""

    def __init__(self, settings: Settings):
        from anthropic import Anthropic

        self._model = settings.reasoning_model
        self._client = Anthropic(api_key=settings.anthropic_api_key)

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]:
        system = (
            "You decompose a software goal into independent, well-scoped issues. "
            f"Return at most {max_subissues} issues as a JSON array of objects with "
            'keys "title", "description", "team". Teams: backend, frontend, qa, '
            "mobile, cloud, data-warehousing, platform. Output JSON only."
        )
        user = f"Goal: {goal.title}\n\nDetails: {goal.description}"
        data = extract_json(self._ask(system, user))
        specs = [
            IssueSpec(
                title=d["title"],
                description=d.get("description", ""),
                team=d.get("team", "backend"),
            )
            for d in data
        ]
        return specs[:max_subissues]

    def plan_issue(self, issue: Issue) -> str:
        system = "Produce a concise implementation plan (3-6 steps) for the issue."
        return self._ask(system, f"Issue: {issue.title}\n\n{issue.description}")

    def gate_review(self, issue, gate_type, recent=None) -> GateReview:
        system = (
            f"You are the reviewer for the '{gate_type}' gate of a 5-phase issue "
            "pipeline (intake, implementation, qa_gate, completion, comms_response). "
            'Decide if the gate passes. Output JSON: {"passed": bool, "reasons": [str]}.'
        )
        user = (
            f"Issue: {issue.title}\nState: {issue.state}\nGate: {gate_type}\n"
            f"Recent events: {json.dumps(recent or [])[:1500]}"
        )
        data = extract_json(self._ask(system, user))
        return GateReview(passed=bool(data.get("passed")),
                          reasons=list(data.get("reasons", [])))

    def score_drift(self, issue, recent=None) -> float:
        system = (
            "Rate how well the issue's recent activity stays aligned with its stated "
            'goal. Output JSON: {"drift_score": float} in [0,1], 1.0 = perfectly '
            "on-track, 0.0 = completely adrift."
        )
        user = (
            f"Issue: {issue.title}\n{issue.description}\n"
            f"Recent events: {json.dumps(recent or [])[:1500]}"
        )
        data = extract_json(self._ask(system, user))
        return float(data.get("drift_score", 1.0))

    def triage_message(self, message: dict[str, Any]) -> TriageDecision:
        system = (
            "You triage an inbound cross-team request for the receiving software "
            "team (PROCESS_GUIDE: the decision to create an issue is always the "
            "receiving team's). Accept if it is actionable work for this team; "
            "reject with a reason otherwise. Output JSON: "
            '{"accept": bool, "title": str, "description": str, "reason": str}.'
        )
        user = (
            f"From: {message['from_team']}\nTo: {message['to_team']}\n"
            f"Priority: {message.get('priority', 'medium')}\n"
            f"Subject: {message['subject']}\n\n{message.get('body', '')}"
        )
        data = extract_json(self._ask(system, user))
        return TriageDecision(
            accept=bool(data.get("accept")),
            title=str(data.get("title") or message["subject"]),
            description=str(data.get("description", "")),
            reason=str(data.get("reason", "")),
        )


def make_reasoner(settings: Settings) -> Reasoner:
    if settings.anthropic_api_key:
        return AnthropicReasoner(settings)
    return StubReasoner()
