#!/usr/bin/env python3
"""Reference PULL-agent poll loop (provider-agnostic).

A long-lived worker that registers as an `external` agent, polls the orchestrator
for work assigned to it, and either edits (dev) or verifies (qa) **in its own
repo**, reporting results back over MCP. The orchestrator never holds or applies
the code — only pointers (commit SHA / test result).

Roles (`--function`):
  dev : at the `implementation` gate, runs the local coder (`--coder`) to edit +
        write tests, then `report_work` (code_committed) and advances the gate.
  qa  : at `verification` runs `--verify`; at `e2e` runs `--verify-e2e`. Reports
        `tests_run` and advances (pass iff the command exits 0). NEVER edits.

Long runs (claude / Playwright take minutes) are kept alive with interleaved
heartbeats from the event loop, so the engine's liveness reclaim doesn't fire
mid-run. Talks to the stdio MCP server (`python -m orchestrator.cli serve`).

Examples:
  # dev coder on the backend
  python loop.py --agent-id 1 --function dev --repo /path/api \
      --coder 'claude -p "{prompt}" --dangerously-skip-permissions'
  # qa runner on the frontend (unit + e2e, gate-aware)
  python loop.py --agent-id 2 --function qa --repo /path/ui \
      --verify 'npm run typecheck' --verify-e2e 'npm run e2e'

Register first: cli register-agent --team backend --function dev --runtime external
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HEARTBEAT_SECONDS = 60  # well under AGENT_STALE_SECONDS

# Which pull gates each role owns. A worker only acts on an issue whose CURRENT
# gate it owns; otherwise it leaves the issue for the engine to reassign to the
# right role (prevents a dev worker from racing ahead through qa/verdict gates
# before reassignment, and vice-versa). Matches pull-1 / pull-fe gate owners.
OWNED_GATES = {"dev": {"implementation"}, "qa": {"verification", "e2e"}}


def _parse(result):
    """Extract the JSON payload from an MCP CallToolResult (structured or text)."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps list/scalar returns as {"result": ...}
        return structured.get("result", structured)
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def build_prompt(issue: dict, adr_rules: str) -> str:
    parts = [
        f"Implement issue #{issue['id']}: {issue['title']}",
        issue.get("description", "") or "",
    ]
    if adr_rules:
        parts.append("\nArchitectural rules you MUST follow:\n" + adr_rules)
    parts.append(
        "\nScope: implement ONLY this issue in the current repository. Write code "
        "AND tests. Make the edits and stop — do NOT run git (the harness commits "
        "for you), do NOT push, and do NOT start a role session or poll "
        "dev_communication/ or pick up other work."
    )
    return "\n".join(p for p in parts if p)


async def _run_with_heartbeats(session, agent_id, *, argv=None, shell_cmd=None,
                               cwd=None) -> int:
    """Run a subprocess, pinging heartbeat() every HEARTBEAT_SECONDS until it exits.
    Returns the process return code."""
    if shell_cmd is not None:
        proc = await asyncio.create_subprocess_shell(shell_cmd, cwd=cwd)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd)
    while True:
        try:
            await asyncio.wait_for(proc.wait(), timeout=HEARTBEAT_SECONDS)
            return proc.returncode
        except asyncio.TimeoutError:
            await session.call_tool("heartbeat", {"agent_id": agent_id})


async def _git(args, cwd) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _handle_dev(session, args, issue):
    adrs = _parse(await session.call_tool("adr_list", {"status": "accepted"})) or []
    adr_rules = "\n".join(f"- {a['decision']}" for a in adrs
                          if isinstance(a, dict) and a.get("decision"))
    prompt = build_prompt(issue, adr_rules)
    argv = [p.format(prompt=prompt) for p in shlex.split(args.coder)]
    await _run_with_heartbeats(session, args.agent_id, argv=argv, cwd=args.repo)
    # The harness owns the commit: capture whatever the coder edited.
    dirty = await _git(["status", "--porcelain"], args.repo)
    if dirty:
        await _git(["add", "-A"], args.repo)
        await _git(["-c", "user.email=orchestrator@local",
                    "-c", "user.name=orchestrator", "commit", "-m",
                    f"issue #{issue['id']}: {issue['title']}"], args.repo)
    sha = await _git(["rev-parse", "HEAD"], args.repo)
    branch = await _git(["rev-parse", "--abbrev-ref", "HEAD"], args.repo)
    await session.call_tool("report_work", {
        "issue_id": issue["id"], "sha": sha, "branch": branch,
        "summary": "changes committed" if dirty else "no file changes made"})
    await session.call_tool("gate_decision", {"issue_id": issue["id"], "passed": True})


async def _handle_qa(session, args, issue):
    gate = issue.get("gate_type")
    cmd = args.verify_e2e if gate == "e2e" else args.verify
    if not cmd:
        print(f"[qa] no verify command for gate {gate!r}; leaving issue "
              f"{issue['id']} for reclaim", file=sys.stderr)
        return
    rc = await _run_with_heartbeats(session, args.agent_id, shell_cmd=cmd, cwd=args.repo)
    passed = rc == 0
    await session.call_tool("append_log", {
        "issue_id": issue["id"], "event_type": "tests_run",
        "payload": {"gate": gate, "passed": passed, "returncode": rc, "cmd": cmd}})
    await session.call_tool("gate_decision",
                            {"issue_id": issue["id"], "passed": passed})


async def poll_once(session, args):
    """Returns (acted, next_poll_seconds). next_poll_seconds is the server's idle
    cadence: None = server has no cadence field (use --interval fallback), 0 =
    looping disabled (stop after the queue drains), >0 = idle wait."""
    hb = _parse(await session.call_tool("heartbeat", {"agent_id": args.agent_id})) or {}
    mine = _parse(await session.call_tool(
        "list_my_work", {"agent_id": args.agent_id})) or []
    owned = OWNED_GATES.get(args.function, set())
    acted = 0
    for issue in mine:
        if issue.get("gate_type") not in owned:
            # assigned to me but not at one of my gates (just advanced across a
            # role boundary) — leave it; the engine reassigns to the right role.
            continue
        if args.function == "dev":
            await _handle_dev(session, args, issue)
        else:
            await _handle_qa(session, args, issue)
        acted += 1
    return acted, hb.get("next_poll_seconds")


async def main_async(args) -> None:
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "orchestrator.cli", "serve"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(f"[pull-agent] agent={args.agent_id} function={args.function} "
                  f"repo={args.repo}", file=sys.stderr)
            while True:
                try:
                    acted, nps = await poll_once(session, args)
                except Exception as exc:  # noqa: BLE001
                    print(f"[pull-agent] poll error: {exc}", file=sys.stderr)
                    acted, nps = 0, None
                if acted:
                    continue                         # keep clearing the queue
                # queue empty: obey the server's cadence
                if nps is None:
                    await asyncio.sleep(args.interval)   # server has no cadence field
                elif nps > 0:
                    await asyncio.sleep(nps)             # loop enabled → idle wait
                else:
                    print("[pull-agent] loop disabled (next_poll_seconds=0); "
                          "queue empty — stopping.", file=sys.stderr)
                    break                                # disabled → stop after drain


def main() -> None:
    ap = argparse.ArgumentParser(description="Reference pull-agent poll loop")
    ap.add_argument("--agent-id", type=int, required=True)
    ap.add_argument("--repo", required=True, help="this worker's own checkout (cwd)")
    ap.add_argument("--function", choices=["dev", "qa"], default="dev")
    ap.add_argument("--coder", default='claude -p "{prompt}" --dangerously-skip-permissions',
                    help="dev: local coder command; {prompt} is substituted")
    ap.add_argument("--verify", default="", help="qa: command for the verification gate")
    ap.add_argument("--verify-e2e", dest="verify_e2e", default="",
                    help="qa: command for the e2e gate")
    ap.add_argument("--interval", type=int, default=15, help="poll seconds when idle")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
