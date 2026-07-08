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
# function -> the concrete "do the work" step for the loop (gate-correct, not dev-default)
_WORK_STEP = {
    "dev": ("`context_load(topic=issue.title)`, then implement the issue (minimal, in-lane) AND "
            "add a test. Commit ONLY the issue's own files on the branch (never push); capture the "
            "sha. Then `report_work(issue_id, sha=…, branch=…, summary=…, tests_passed=true)` and "
            "`gate_decision(issue_id, passed=true)`."),
    "qa": ("**FIRST build a clean slate — never reuse a previous cycle's tree:** "
           "`git checkout -B _verify-<issue_id> <issue_branch>` (force-reset a THROWAWAY branch to "
           "the issue's branch tip, which the dev has already synced with current `main`; `-B` also "
           "sidesteps the 'branch checked out in another worktree' lock). `<issue_branch>` is the "
           "`branch` the dev reported (convention `issue-<id>`). Do NOT keep a long-lived branch or "
           "`git merge main` into one — that accumulates merge commits and can permanently bake in "
           "corruption. THEN run the gate's verify command in this checkout (verification → "
           "typecheck/unit/build; e2e → Playwright), capturing its exit code. Then "
           "`report_work(issue_id, summary=…, tests_passed=<exit==0>)` and "
           "`gate_decision(issue_id, passed=<exit==0>)`. You do NOT edit application code — you run "
           "the checks and report the result."),
    "lead": ("review the diff + test evidence (report_work / tests_run payloads) and render "
             "`gate_decision(issue_id, passed=…)`. You do NOT edit the repo."),
}
# function -> the loop's step-2 "sync" line. Dev owns its issue branch and merges main into it;
# QA builds a fresh ephemeral verify branch PER ISSUE (in the work step), so it has no persistent
# branch to pre-sync; leads render verdicts over MCP and have no working tree.
_SYNC_STEP = {
    "dev": ("**Sync the integration branch FIRST — `git merge --no-edit main` into your issue "
            "branch.** This is how cross-team work reaches you: landed API contracts "
            "(`packages/contracts`), shared test/gate config, and other teams' merged changes live "
            "on `main`, and you only see them after merging it in. Do this every cycle before "
            "working so you build on current `main`, not a stale fork. Conflicts: prefer `main` for "
            "shared contract/config files; for a genuine code conflict resolve minimally or report "
            "a blocker via `comms_send`. LOCAL only — never push."),
    "qa": ("**Do NOT keep a long-lived branch or pre-merge `main` here.** This worktree is "
           "EPHEMERAL: you rebuild a clean verify branch from the issue's own branch for each issue "
           "in step 5 (force-reset, discards the prior cycle), so nothing accumulates and a "
           "transient corruption can never become permanent. The issue branch already carries "
           "current `main` (the dev merged it). LOCAL only — never push."),
    "lead": ("**No working tree to sync** — you render verdicts over MCP from the reported "
             "evidence; there is no branch to merge."),
}
# function -> one extra boundary line (gate-correct)
_BOUNDARY = {
    "dev": "Add a test for every implementation; keep changes minimal and conventional.",
    "qa": "You run verify commands and report results — never edit application code or act on another gate.",
    "lead": "You render verdicts only — never edit the repo.",
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
2. {sync_step}
3. `mcp__orchestrator__list_my_work(agent_id={agent_id})` — your assigned in-progress issues. Act ONLY on your gate(s): **{owned_gates}**. (If an assigned issue is at a different gate, it's not yours to work — leave it.)
4. `mcp__orchestrator__adr_list(status="accepted")` — the live rules (authoritative; honor every cycle).
5. Process your assigned issues **one at a time** (do NOT batch-claim and run silently). For each: {work_step}
6. **Heartbeat WHILE you work — this is mandatory, not optional.** Call `mcp__orchestrator__heartbeat(agent_id={agent_id})` at the start of every issue and **again at least every ~2 minutes during any long-running step** (test suite, build, e2e). A worker that goes silent past the stale window is treated as dead: its issue is reclaimed mid-run and, after a few reclaims, quarantined (off_rails). Frequent heartbeats are what keep your work yours.
7. When the queue is empty, obey `next_poll_seconds` (loop enabled → keep polling at that cadence and pick up newly-assigned work; disabled → slow-poll, never fully stop).

## Boundaries
- Act only on issues assigned to agent_id {agent_id}, and only at your gate(s): **{owned_gates}**.
- {boundary_extra}
- Never act on a gate you don't own (don't implement if you're QA; don't verify if you're dev; don't edit the repo if you're lead).
- Never `push`; touch only the issue's own files.
- **Do NOT merge or promote your work to `main`, and do NOT send "promotion-request" messages.** When your issue reaches completion the orchestrator AUTOMATICALLY merges your branch into `main` and records a `promoted` event (a human reviews these after the fact). Your responsibility ends at `gate_decision` — there is no manual merge/promote step, and trying to do one will get you stuck.

{rules_block}
"""


def render_agent_doc(*, vendor: str, team: str, function: str, agent_id: int,
                     rules: list[dict[str, Any]]) -> str:
    """Render one vendor's instruction file from the protocol template + rules."""
    owned_gates, owned_desc = _OWNED.get(function, ("(none)", "—"))
    work_step = _WORK_STEP.get(function, "do your gate's work, then report_work + gate_decision.")
    sync_step = _SYNC_STEP.get(function, _SYNC_STEP["dev"])
    boundary_extra = _BOUNDARY.get(function, "Keep changes minimal and in-lane.")
    rules_block = adr_rules.format_rules_block(rules) or \
        "## Applicable rules (from the orchestrator SoT)\n\n_(no accepted ADRs apply yet)_"
    return _TEMPLATE.format(
        header=GENERATED_HEADER, team=team, function=function, agent_id=agent_id,
        bootstrap=_BOOTSTRAP.get(vendor, ""), owned_gates=owned_gates,
        owned_desc=owned_desc, work_step=work_step, sync_step=sync_step,
        boundary_extra=boundary_extra, rules_block=rules_block,
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
