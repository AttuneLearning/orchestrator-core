"""ADR governance: pure rule selection and formatting.

Rules live in the adrs table (see repository.list_adrs). A rule's applies_to
selector has optional dimensions {work_types, teams, repos}; an empty/missing
dimension matches everything, so `repos: []` means project-wide. Selection is
the intersection of all dimensions — each agent receives the most concise
applicable list by construction.

Only `decision` (the compact directive) is ever shipped to agents; `context`
(rationale) stays human-side. No I/O here: callers pass rule dicts in.
"""

from __future__ import annotations

from typing import Any, Optional

# Work-type detection, ported from the agent-workflow /context skill's
# keyword table. First matching bucket wins; order is the priority.
WORK_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("auth-change", ["auth", "permission", "access control", "role", "login", "rbac"]),
    ("new-endpoint", ["route", "endpoint", "api", "controller", "rest"]),
    ("new-model", ["model", "schema", "collection", "migration", "table"]),
    ("testing", ["test", "spec", "coverage", "jest", "pytest"]),
    ("bug-fix", ["fix", "bug", "broken", "error", "crash", "regression"]),
]

GENERAL = "general"


def detect_work_type(text: str) -> str:
    """Mechanical keyword classification of an issue's title+description."""
    lowered = text.lower()
    for work_type, keywords in WORK_TYPE_KEYWORDS:
        if any(k in lowered for k in keywords):
            return work_type
    return GENERAL


def _dim_matches(selector: Any, value: str) -> bool:
    """Empty/missing selector dimension matches everything."""
    if not selector:
        return True
    return value in selector


def _repos_match(rule_repos: Any, issue_repos: list[str]) -> bool:
    """Empty rule.repos = project-wide (always applies). A repo-scoped rule
    applies only when the issue's team works in one of those repos; teams with
    no repo mapping get project-wide rules only (keeps their list concise)."""
    if not rule_repos:
        return True
    return bool(set(rule_repos) & set(issue_repos))


def applicable(
    rules: list[dict[str, Any]],
    *,
    work_type: Optional[str],
    team: str,
    repos: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Accepted rules whose selector matches this issue's coordinates."""
    out = []
    for rule in rules:
        if rule.get("status") != "accepted":
            continue
        sel = rule.get("applies_to") or {}
        if not _dim_matches(sel.get("work_types"), work_type or GENERAL):
            continue
        if not _dim_matches(sel.get("teams"), team):
            continue
        if not _repos_match(sel.get("repos"), repos or []):
            continue
        out.append(rule)
    return out


def format_rules_block(rules: list[dict[str, Any]]) -> str:
    """The condensed slice handed to an agent: one directive line per rule."""
    if not rules:
        return ""
    lines = "\n".join(f"- [{r['adr_key']}] {r['decision']}" for r in rules)
    return (
        "## Applicable rules (follow each; a gate review will verify and "
        "cite the rule id when declining)\n" + lines
    )


def reverse_links(rules: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Incoming backlinks per adr_key, computed from related+supersedes edges."""
    incoming: dict[str, list[str]] = {}
    for rule in rules:
        for target in list(rule.get("related") or []) + list(rule.get("supersedes") or []):
            incoming.setdefault(target, []).append(rule["adr_key"])
    return incoming
