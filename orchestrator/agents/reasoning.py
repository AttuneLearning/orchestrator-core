"""Reasoning agent.

Makes the engine's structured decisions:

    decompose_goal(goal)        -> list[IssueSpec]
    plan_issue(issue)           -> str
    gate_review(issue, gate)    -> GateReview
    score_drift(issue, events)  -> float   (1.0 = perfectly on-track, 0.0 = adrift)
    triage_message / assess_complexity / suggest_adr  (optional capabilities)

Backends (select via REASONER, else auto):
  * stub      — deterministic, network-free (default when no key/provider).
  * anthropic — Anthropic API (ANTHROPIC_API_KEY; metered, NOT the subscription).
  * openai    — any OpenAI-compatible endpoint (REASONER_BASE_URL/MODEL/API_KEY),
                e.g. a locally hosted model.
  * cli       — shells out to a local coder CLI (`claude -p ...`), so it runs on
                your Claude subscription with no API key. Mirrors CliSessionWorker.

The model backends share all prompts/parsing in _LLMReasoner; each only implements
_ask(system, user) -> str. Capabilities stay duck-typed (engine uses getattr), so
older reasoners keep working.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from typing import Any, Optional, Protocol

# METHOD /path tokens in free text, e.g. "GET /system/status". Used by the
# deterministic stub's extract_endpoint_deps and as a fallback shape.
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)", re.IGNORECASE)


def _parse_endpoints(text: str) -> list[dict[str, str]]:
    """Extract unique {method, path} endpoint references from free text."""
    seen: list[dict[str, str]] = []
    for method, path in _ENDPOINT_RE.findall(text or ""):
        dep = {"method": method.upper(), "path": path.rstrip(".,;)")}
        if dep not in seen:
            seen.append(dep)
    return seen

from ..config import Settings
from ..models import Goal, Issue
from .base import (ComplexityAssessment, GateReview, IssueSpec, TriageDecision,
                   extract_json)


class Reasoner(Protocol):
    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]: ...
    def plan_issue(self, issue: Issue, rules: str = "") -> str: ...
    def gate_review(self, issue: Issue, gate_type: str,
                    recent: Optional[list[dict[str, Any]]] = None,
                    rules: str = "") -> GateReview: ...
    def score_drift(self, issue: Issue,
                    recent: Optional[list[dict[str, Any]]] = None) -> float: ...
    def triage_message(self, message: dict[str, Any]) -> TriageDecision: ...
    def assess_complexity(self, issue: Issue) -> ComplexityAssessment: ...
    def suggest_adr(self, issue: Issue) -> Optional[dict[str, Any]]: ...
    def extract_endpoint_deps(self, issue: Issue) -> list[dict[str, str]]: ...
    def draft_reply(self, message: dict[str, Any]) -> str: ...
    def review_reply(self, message: dict[str, Any], context: str, draft: str) -> str: ...


class StubReasoner:
    """Deterministic, network-free reasoner for hermetic runs and tests."""

    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]:
        # One implementation issue. Verification (tests / typecheck / e2e) is an
        # acceptance criterion the QA runner clears at its gates — never its own
        # sub-issue (a separate "Test: …" issue just duplicates the QA gate).
        return [IssueSpec(title=f"Implement: {goal.title}",
                          description=goal.description)][:max_subissues]

    def plan_issue(self, issue: Issue, rules: str = "") -> str:
        return f"Plan for '{issue.title}': implement, add tests, verify, complete."

    def gate_review(self, issue, gate_type, recent=None, rules: str = "") -> GateReview:
        # Stub passes every gate so issues flow to done in the happy path.
        return GateReview(passed=True, reasons=[f"{gate_type} exit criteria met (stub)"])

    def score_drift(self, issue, recent=None) -> float:
        return 1.0

    def triage_message(self, message: dict[str, Any]) -> TriageDecision:
        # Stub accepts every inbound request; title derives from the subject.
        return TriageDecision(accept=True, title=message["subject"],
                              description=message.get("body", ""))

    def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
        # Stub never decomposes, keeping hermetic runs simple and bounded;
        # decomposition paths are exercised by tests with an injected reasoner.
        return ComplexityAssessment(decompose=False, subissues=[])

    def suggest_adr(self, issue: Issue) -> Optional[dict[str, Any]]:
        # Stub never proposes rules; gap-detection paths are test-injected.
        return None

    def extract_endpoint_deps(self, issue: Issue) -> list[dict[str, str]]:
        # Deterministic: pull "METHOD /path" tokens out of the issue text.
        return _parse_endpoints(f"{issue.title}\n{issue.description}")

    def draft_reply(self, message: dict[str, Any], context: str = "") -> str:
        # Deterministic placeholder; the dashboard human reviews/overrides it.
        # Echoes whether grounding context was supplied (used by tests).
        tag = "[draft+ctx]" if context else "[draft]"
        return (f"{tag} Re: {message.get('subject', '')} — "
                f"acknowledged from {message.get('from_team', '?')}.")

    def review_reply(self, message: dict[str, Any], context: str = "",
                     draft: str = "") -> str:
        # Deterministic QA pass; tags the draft so tests can see 2-pass ran.
        return f"[qa] {draft}"


class _LLMReasoner:
    """Shared prompts + JSON parsing for every model-backed reasoner. Subclasses
    implement _ask(system, user, max_tokens) -> str."""

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:  # pragma: no cover
        raise NotImplementedError

    def decompose_goal(self, goal: Goal, max_subissues: int) -> list[IssueSpec]:
        system = (
            "You decompose a software goal into independent, well-scoped issues. "
            f"Return at most {max_subissues} issues as a JSON array of objects with "
            'keys "title", "description", "team". Teams: backend, frontend, qa, '
            "mobile, cloud, data-warehousing, platform. Choose the team that owns "
            "the work (a UI/frontend goal -> frontend, an API goal -> backend). "
            "Output JSON only."
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

    def plan_issue(self, issue: Issue, rules: str = "") -> str:
        system = "Produce a concise implementation plan (3-6 steps) for the issue."
        user = f"Issue: {issue.title}\n\n{issue.description}"
        if rules:
            user += f"\n\n{rules}"
        return self._ask(system, user)

    def gate_review(self, issue, gate_type, recent=None, rules: str = "") -> GateReview:
        system = (
            f"You are the reviewer for the '{gate_type}' gate of an issue pipeline. "
            "Decide if the gate passes. If applicable rules are listed, verify "
            "each one; a violated rule fails the gate and its id goes in "
            '"violated_rules". Output JSON: {"passed": bool, "reasons": [str], '
            '"violated_rules": [str]}.'
        )
        user = (
            f"Issue: {issue.title}\nState: {issue.state}\nGate: {gate_type}\n"
            f"Recent events: {json.dumps(recent or [])[:1500]}"
        )
        if rules:
            user += f"\n\n{rules}"
        data = extract_json(self._ask(system, user))
        return GateReview(passed=bool(data.get("passed")),
                          reasons=list(data.get("reasons", [])),
                          violated_rules=list(data.get("violated_rules", [])))

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

    def assess_complexity(self, issue: Issue) -> ComplexityAssessment:
        system = (
            "You are the architect. Decide if this issue is too large for one "
            "agent in one pipeline pass and should be decomposed into smaller "
            "sub-issues (each independently completable). Decompose only when "
            "genuinely necessary. Output JSON: "
            '{"decompose": bool, "subissues": [{"title": str, "description": str}]}.'
        )
        user = f"Issue: {issue.title}\n\n{issue.description}"
        data = extract_json(self._ask(system, user))
        specs = [
            IssueSpec(title=d["title"], description=d.get("description", ""))
            for d in data.get("subissues", [])
        ]
        return ComplexityAssessment(decompose=bool(data.get("decompose")) and bool(specs),
                                    subissues=specs)

    def suggest_adr(self, issue: Issue) -> Optional[dict[str, Any]]:
        system = (
            "An issue completed with NO architecture rules governing its kind of "
            "work. Decide whether a reusable decision should be recorded. Only "
            "propose genuinely reusable, project-level decisions — not one-off "
            "details. Output JSON: null, or {\"domain\": str, \"title\": str, "
            '"decision": str (ONE imperative sentence agents will follow), '
            '"context": str (why), "applies_to": {"work_types": [str], '
            '"teams": [str], "repos": [str]}}.'
        )
        user = (f"Issue: {issue.title}\nTeam: {issue.team}\n"
                f"Work type: {issue.work_type}\n\n{issue.description}")
        data = extract_json(self._ask(system, user))
        if not data or not isinstance(data, dict) or not data.get("decision"):
            return None
        return data

    def extract_endpoint_deps(self, issue: Issue) -> list[dict[str, str]]:
        system = (
            "List the backend API endpoints this frontend issue must CONSUME "
            "(call) to be implemented — not endpoints it defines. Output JSON: "
            '{"endpoints": [{"method": str, "path": str}]} with HTTP method '
            "uppercase and path like /system/status. Empty list if none."
        )
        user = f"Issue: {issue.title}\nTeam: {issue.team}\n\n{issue.description}"
        try:
            data = extract_json(self._ask(system, user))
        except Exception:  # noqa: BLE001 - degrade to text parsing on bad JSON
            return _parse_endpoints(f"{issue.title}\n{issue.description}")
        out: list[dict[str, str]] = []
        for d in (data.get("endpoints", []) if isinstance(data, dict) else []):
            method = str(d.get("method", "")).upper().strip()
            path = str(d.get("path", "")).strip()
            if method and path:
                dep = {"method": method, "path": path}
                if dep not in out:
                    out.append(dep)
        return out

    def draft_reply(self, message: dict[str, Any], context: str = "") -> str:
        system = (
            "You are the orchestration monitor drafting a reply to an inbound "
            "cross-team question about how the orchestration process / system works. "
            "Ground your answer ONLY in the supplied orchestration reference; if the "
            "reference does not cover it, say so rather than guessing. A human will "
            "review and may override your draft. Be concise and concrete. Plain text."
        )
        ref = (f"Orchestration reference (authoritative):\n{context}\n\n"
               if context else "")
        user = (
            f"{ref}"
            f"From: {message.get('from_team')}\nTo: {message.get('to_team')}\n"
            f"Priority: {message.get('priority', 'medium')}\n"
            f"Subject: {message.get('subject', '')}\n\n{message.get('body', '')}"
        )
        return self._ask(system, user, max_tokens=700)

    def review_reply(self, message: dict[str, Any], context: str = "",
                     draft: str = "") -> str:
        system = (
            "You are QA-reviewing a draft reply to a cross-team question, checking it "
            "against the authoritative orchestration reference. Verify EACH factual "
            "claim against the reference. Correct anything inaccurate; remove or flag "
            "claims the reference does not support (do not invent). Pay attention to "
            "exact field names, which fields are required vs optional/defaulted, "
            "allowed enum values, and precise computations. Output ONLY the corrected "
            "final reply (plain text) — no commentary about the review."
        )
        user = (
            f"Reference (authoritative):\n{context}\n\n"
            f"Question:\n{message.get('subject', '')}\n{message.get('body', '')}\n\n"
            f"Draft reply to review and correct:\n{draft}"
        )
        return self._ask(system, user, max_tokens=900)


class AnthropicReasoner(_LLMReasoner):
    """Structured decisions via the Anthropic API (metered; ANTHROPIC_API_KEY)."""

    def __init__(self, settings: Settings):
        from anthropic import Anthropic

        self._model = settings.reasoning_model
        self._client = Anthropic(api_key=settings.anthropic_api_key)

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        resp = self._client.messages.create(
            model=self._model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class OpenAIReasoner(_LLMReasoner):
    """Structured decisions via any OpenAI-compatible endpoint (e.g. a locally
    hosted model). Configure REASONER_BASE_URL / REASONER_MODEL / REASONER_API_KEY."""

    def __init__(self, settings: Settings):
        from openai import OpenAI

        self._model = settings.reasoner_model or settings.reasoning_model
        self._client = OpenAI(
            base_url=settings.reasoner_base_url or None,
            api_key=settings.reasoner_api_key or "not-needed",
        )

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        resp = self._client.chat.completions.create(
            model=self._model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


class CliReasoner(_LLMReasoner):
    """Structured decisions via a local coder CLI (default `claude -p "{prompt}"`),
    running on your Claude subscription — no API key. The command template's
    {prompt} placeholder receives the combined system+user prompt; stdout is parsed
    as the model's response. Runs in a scratch cwd so it doesn't load a project
    CLAUDE.md."""

    _TIMEOUT_S = 180

    def __init__(self, settings: Settings):
        self._cmd = settings.reasoner_cli_cmd or 'claude -p "{prompt}"'
        self._cwd = tempfile.mkdtemp(prefix="orch-reasoner-")

    def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        prompt = f"{system}\n\n{user}\n\nReturn only the requested output."
        argv = [part.format(prompt=prompt) for part in shlex.split(self._cmd)]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=self._TIMEOUT_S, cwd=self._cwd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"cli reasoner exited {proc.returncode}: {proc.stderr[:300]}")
        return proc.stdout.strip()


def make_reasoner(settings: Settings) -> Reasoner:
    """Select the reasoner backend. REASONER overrides; otherwise auto: anthropic
    when a key is present, else the deterministic stub."""
    provider = (settings.reasoner or "").lower().strip()
    if not provider:
        provider = "anthropic" if settings.anthropic_api_key else "stub"
    if provider == "stub":
        return StubReasoner()
    if provider == "anthropic":
        return AnthropicReasoner(settings)
    if provider == "openai":
        return OpenAIReasoner(settings)
    if provider == "cli":
        return CliReasoner(settings)
    raise ValueError(f"unknown REASONER provider {provider!r} "
                     "(expected stub|anthropic|openai|cli)")
