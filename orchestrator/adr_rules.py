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

# Work-type detection, ported from the upstream agent-workflow /context skill's
# keyword table (github.com/AttuneLearning/agent-workflow @ 555ff00).
# First matching bucket wins; order is the priority.
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


def closure(seed_keys: Any, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every rule reachable from `seed_keys` by following each ADR's OWN curated
    `related`/`supersedes` edges forward, plus the seeds themselves.

    NO depth cap: an ADR is never dropped for being "too far" — the full forward
    chain is walked. Traversal is forward-only ON PURPOSE: an ADR names the other
    rules relevant to it, so we pull those; we do NOT reverse-pull every rule that
    merely points AT a seed (that leaks unrelated rules in through shared nodes).
    Efficiency comes from a small, precise seed (selector match + reasoner tags)
    and a sparse, curated edge set — not from truncation.
    """
    by_key = {r["adr_key"]: r for r in rules}
    adj: dict[str, set[str]] = {}
    for r in rules:
        k = r["adr_key"]
        edges = list(r.get("related") or []) + list(r.get("supersedes") or [])
        adj[k] = {t for t in edges}  # forward edges only (curated intent)
    seen: set[str] = set()
    stack = [k for k in seed_keys if k in by_key]
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        for nb in adj.get(k, ()):  # noqa: SIM118
            if nb in by_key and nb not in seen:
                stack.append(nb)
    return [by_key[k] for k in sorted(seen)]


def _universal_keys(accepted: list[dict[str, Any]]) -> set[str]:
    """Project-wide governance: rules with no team AND no work_type selector, i.e.
    they apply to every issue (contracts-first, testing, verification, HIPAA, PHI
    minimization…). Always present so precise reasoner tagging never drops them."""
    out = set()
    for r in accepted:
        sel = r.get("applies_to") or {}
        if not sel.get("teams") and not sel.get("work_types"):
            out.add(r["adr_key"])
    return out


def relevant(
    rules: list[dict[str, Any]],
    *,
    work_type: Optional[str],
    team: str,
    repos: Optional[list[str]] = None,
    extra_keys: Any = (),
) -> list[dict[str, Any]]:
    """The ADR surface for one issue, uncapped and deterministic:

    * If the reasoner tagged this issue (`extra_keys`), TRUST those precise tags
      plus the universal project-wide floor — a small, on-point surface.
    * Otherwise fall back to the deterministic team/work_type selector match.

    Either seed is then expanded via the full forward backlink `closure` (no depth
    cap). Efficiency = a precise seed + a sparse curated graph; completeness = the
    universal floor + closure, so nothing relevant is ignored."""
    accepted = [r for r in rules if r.get("status") == "accepted"]
    valid = {r["adr_key"] for r in accepted}
    tags = {k for k in (extra_keys or ()) if k in valid}
    if tags:
        seed = tags | _universal_keys(accepted)
    else:
        seed = {r["adr_key"] for r in applicable(accepted, work_type=work_type,
                                                 team=team, repos=repos)}
    return closure(seed, accepted)
