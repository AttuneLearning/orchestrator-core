"""Render per-agent instruction files (CLAUDE.md / AGENTS.md) from the orchestrator
single source of truth (accepted ADRs + a protocol template).

The orchestrator is the SoT; these files are GENERATED artifacts, identical in body
across vendors (only the bootstrap line differs). A drift check (render in memory,
diff the committed file) keeps a hand-edited file from lying about the protocol.
Pure except for the thin pool-backed `render_for` fetcher.
"""

from __future__ import annotations

from typing import Any, Optional

from . import adr_rules

GENERATED_HEADER = (
    "<!-- GENERATED FROM ORCHESTRATOR SoT — DO NOT EDIT BY HAND. "
    "Regenerate: python -m orchestrator.cli render-agent-docs ... -->"
)

# function -> (gates it owns, one-line of what doing the work means)
_OWNED = {
    "dev": ("implementation", "edit code AND add a test, commit on the branch, "
            "then report_work + gate_decision(passed=true)"),
    "qa": ("verification + e2e", "run the gate's verify command, report tests_run, "
           "then gate_decision(passed = command exited 0)"),
    "lead": ("(verdict gates only)", "review the diff + test evidence and render "
             "gate_decision — you do not edit the repo"),
}
_BOOTSTRAP = {
    "claude": "Claude Code loads this file as project instructions.",
    "codex": "Codex / AGENTS-aware tools load this file.",
}
_FILENAME = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}

_TEMPLATE = """{header}
# Orchestrator worker — {team}_{function}

You are a **pull worker** for the multi-agent orchestrator. {bootstrap}
Your identity is fixed:

- **agent_id: {agent_id}** (team `{team}`, function `{function}`)
- **repo:** this checkout. **Never push.**
- **You own the {owned_gates} gate(s)** — {owned_desc}.

The orchestrator is the **single source of truth**. The rules below are generated
from its accepted ADRs; the authoritative live copy reaches you over MCP
(`adr_list` / `context_load`) every cycle. Do **not** hand-edit this file — it is
regenerated from the orchestrator, and a drift check will reject manual edits.

## Loop (each cycle)
1. `mcp__orchestrator__heartbeat(agent_id={agent_id})` — liveness + cadence (`next_poll_seconds`).
2. `mcp__orchestrator__list_my_work(agent_id={agent_id})` — your assigned in-progress issues; act ONLY on your gate.
3. `mcp__orchestrator__adr_list(status="accepted")` — the live rules (authoritative; honor every cycle).
4. Do the work in this repo: minimal, conventional, with a test. Commit on the branch; never push.
5. `report_work(...)` then `gate_decision(...)`. Clear the queue, then obey `next_poll_seconds`
   (loop enabled → keep polling at that cadence; disabled → stop after the queue empties).

## Boundaries
- Act only on issues assigned to agent_id {agent_id}, and only at your gate(s).
- Keep changes minimal and conventional; add a test for every implementation.
- Never `push`; commit only the issue's own files.

{rules_block}
"""


def render_agent_doc(*, vendor: str, team: str, function: str, agent_id: int,
                     rules: list[dict[str, Any]]) -> str:
    """Render one vendor's instruction file from the protocol template + rules."""
    owned_gates, owned_desc = _OWNED.get(function, ("(none)", "—"))
    rules_block = adr_rules.format_rules_block(rules) or \
        "## Applicable rules (from the orchestrator SoT)\n\n_(no accepted ADRs apply yet)_"
    return _TEMPLATE.format(
        header=GENERATED_HEADER, team=team, function=function, agent_id=agent_id,
        bootstrap=_BOOTSTRAP.get(vendor, ""), owned_gates=owned_gates,
        owned_desc=owned_desc, rules_block=rules_block,
    )


def filename_for(vendor: str) -> str:
    return _FILENAME[vendor]


def render_for(pool, roster, team: str, function: str, agent_id: int,
               vendors=("claude", "codex")) -> dict[str, str]:
    """Fetch the applicable accepted ADRs for (team, its repos) and render each
    vendor's file. Returns {filename: content}. (The thin DB-backed entry point.)"""
    from . import repository as repo
    accepted = repo.list_adrs(pool, status="accepted")
    t = roster.resolve(team)
    repos = list(t.repos) if t else []
    applicable = adr_rules.applicable(accepted, work_type=None, team=team, repos=repos)
    out = {}
    for v in vendors:
        out[filename_for(v)] = render_agent_doc(vendor=v, team=team,
                                                function=function, agent_id=agent_id,
                                                rules=applicable)
    return out


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def drift(rendered: str, existing: Optional[str]) -> bool:
    """True if the committed file differs from the freshly-rendered SoT output."""
    if existing is None:
        return True
    return _normalize(rendered) != _normalize(existing)
