"""Server-rendered HTML for the ops dashboard.

Plain-Python rendering (html.escape + f-strings) — no template-engine dependency,
matching the orchestrator's minimal-deps posture. Each function returns an HTML
string; page() wraps a body fragment in the shared shell.
"""

from __future__ import annotations

from html import escape
from typing import Any

_CSS = """
  :root { --bg:#0f1115; --panel:#1a1d24; --ink:#e6e6e6; --muted:#8a91a0;
          --line:#2a2f3a; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --link:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
  header { padding:12px 20px; border-bottom:1px solid var(--line); background:var(--panel); }
  header nav a { margin-right:18px; font-weight:600; }
  main { padding:20px; max-width:1100px; margin:0 auto; }
  h1 { font-size:18px; margin:0 0 16px; } h2 { font-size:15px; margin:24px 0 8px; }
  table { width:100%; border-collapse:collapse; margin:8px 0; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
          background:var(--line); color:var(--ink); }
  .s-done { color:var(--ok); } .s-paused,.s-off_rails,.s-failed { color:var(--bad); }
  .s-blocked { color:var(--warn); }
  .banner { padding:10px 14px; border-radius:6px; margin-bottom:16px; font-weight:600;
            background:rgba(248,81,73,.15); border:1px solid var(--bad); color:var(--bad); }
  .cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:8px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:12px 16px; min-width:120px; }
  .card .n { font-size:24px; font-weight:700; } .card .l { color:var(--muted); font-size:12px; }
  .muted { color:var(--muted); }
  .ev { border-left:2px solid var(--line); padding:4px 0 4px 12px; margin-left:4px; }
  pre { background:#0b0d11; border:1px solid var(--line); border-radius:6px;
        padding:10px; overflow:auto; white-space:pre-wrap; }
  button { font:inherit; background:var(--bad); color:#fff; border:0; border-radius:5px;
           padding:5px 12px; cursor:pointer; } button.alt { background:var(--warn); }
  form { display:inline; }
"""


def _state(s: str) -> str:
    return f'<span class="pill s-{escape(s)}">{escape(s)}</span>'


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)} · orchestrator</title><style>{_CSS}</style></head><body>"
        "<header><nav>"
        "<a href='/'>Fleet</a><a href='/agents'>Agents</a>"
        "<a href='/adrs'>ADRs</a>"
        "<a href='/api/state'>JSON</a>"
        "</nav></header><main>"
        f"{body}"
        "</main></body></html>"
    )


def _counts_cards(label: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"<p class='muted'>No {escape(label)}.</p>"
    cards = "".join(
        f"<div class='card'><div class='n'>{n}</div>"
        f"<div class='l'>{escape(state)}</div></div>"
        for state, n in sorted(counts.items())
    )
    return f"<div class='cards'>{cards}</div>"


def overview(summary: dict[str, Any]) -> str:
    pct = round(summary["fleet_focus"] * 100)
    paused = summary.get("paused_goals", [])
    banner = ""
    if summary["below_threshold"]:
        bits = []
        if summary["flagged"]:
            bits.append(f"{summary['flagged']} of {summary['active_issues']} issues flagged")
        if paused:
            bits.append(f"{len(paused)} goal(s) paused")
        banner = (
            f"<div class='banner'>⚠ Fleet focus {pct}% — {'; '.join(bits)}. "
            "Review and issue a directive below.</div>"
        )

    paused_html = ""
    if paused:
        prows = "".join(
            f"<tr><td><a href='/goals/{g['id']}'>#{g['id']}</a></td>"
            f"<td>{escape(g['title'])}</td>"
            f"<td><form method='post' action='/goals/{g['id']}/resume'>"
            "<button class='alt'>Resume</button></form></td></tr>"
            for g in paused
        )
        paused_html = (
            "<h2>Paused goals</h2><table><tr><th>Goal</th><th>Title</th>"
            f"<th>Action</th></tr>{prows}</table>"
        )

    flagged_rows = "".join(
        f"<tr><td><a href='/issues/{i['id']}'>#{i['id']}</a></td>"
        f"<td>{_state(i['state'])}</td>"
        f"<td>{', '.join(escape(s) for s in i['signals']) or '—'}</td>"
        f"<td>{escape(i['title'])}</td></tr>"
        for i in summary["flagged_issues"]
    )
    flagged = (
        "<h2>Flagged issues</h2><table><tr><th>Issue</th><th>State</th>"
        f"<th>Signals</th><th>Title</th></tr>{flagged_rows}</table>"
        if flagged_rows else "<h2>Flagged issues</h2><p class='muted'>None — all clear.</p>"
    )

    goal_rows = "".join(
        f"<tr><td><a href='/goals/{g['id']}'>#{g['id']}</a></td>"
        f"<td>{_state(g['state'])}</td><td>{escape(g['title'])}</td>"
        f"<td>{g['issue_count']}</td></tr>"
        for g in summary["goals_list"]
    )
    goals = (
        "<h2>Goals</h2><table><tr><th>Goal</th><th>State</th><th>Title</th>"
        f"<th>Issues</th></tr>{goal_rows}</table>"
        if goal_rows else "<h2>Goals</h2><p class='muted'>No goals yet.</p>"
    )

    return page("Fleet", (
        f"<h1>Fleet overview · focus {pct}%</h1>{banner}"
        f"<h2>Goals by state</h2>{_counts_cards('goals', summary['goals'])}"
        f"<h2>Issues by state</h2>{_counts_cards('issues', summary['issues'])}"
        f"{paused_html}{flagged}{goals}"
    ))


def goal_detail(goal: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    resume = ""
    if goal["state"] == "paused":
        resume = (
            f"<form method='post' action='/goals/{goal['id']}/resume'>"
            "<button class='alt'>Resume goal</button></form>"
        )
    rows = "".join(
        f"<tr><td style='padding-left:{12 + i['depth'] * 20}px'>"
        f"<a href='/issues/{i['id']}'>#{i['id']}</a></td>"
        f"<td>{_state(i['state'])}</td>"
        f"<td>{escape(i['gate_type'] or '—')}</td>"
        f"<td>{i['retry_count']}</td><td>{i['step_count']}</td>"
        f"<td>{escape(i['title'])}</td></tr>"
        for i in issues
    )
    return page(f"Goal #{goal['id']}", (
        f"<h1>Goal #{goal['id']}: {escape(goal['title'])} {_state(goal['state'])}</h1>"
        f"{resume}"
        f"<p class='muted'>{escape(goal['description'] or '')}</p>"
        "<h2>Issues</h2><table><tr><th>Issue</th><th>State</th><th>Gate</th>"
        f"<th>Retry</th><th>Step</th><th>Title</th></tr>{rows}</table>"
    ))


def issue_detail(issue: dict[str, Any], events: list[dict[str, Any]]) -> str:
    import json

    directive = ""
    if issue["state"] == "off_rails":
        directive = (
            f"<form method='post' action='/issues/{issue['id']}/directive'>"
            "<button>Resume (clear quarantine)</button></form>"
        )
    ev_html = "".join(
        f"<div class='ev'><strong>{e['seq']}. {escape(e['event_type'])}</strong> "
        f"{escape((e['from_state'] or '') + ' → ' + (e['to_state'] or '')) if e['to_state'] else ''}"
        f"<div class='muted'>{escape(json.dumps(e['payload']))[:600]}</div></div>"
        for e in events
    )
    return page(f"Issue #{issue['id']}", (
        f"<h1>Issue #{issue['id']}: {escape(issue['title'])} {_state(issue['state'])}</h1>"
        f"<p class='muted'>goal <a href='/goals/{issue['goal_id']}'>#{issue['goal_id']}</a> · "
        f"team {escape(issue['team'])} · gate {escape(issue['gate_type'] or '—')} · "
        f"retry {issue['retry_count']} · step {issue['step_count']} · "
        f"depth {issue['depth']}</p>"
        f"{directive}"
        f"<p>{escape(issue['description'] or '')}</p>"
        f"<h2>Timeline ({len(events)} events)</h2>{ev_html or '<p class=muted>No events.</p>'}"
    ))


def adrs_page(adrs: list[dict[str, Any]]) -> str:
    def section(title: str, items: list[dict[str, Any]], approvable: bool) -> str:
        if not items:
            return ""
        rows = "".join(
            f"<tr><td><a href='/adrs/{escape(a['adr_key'])}'>{escape(a['adr_key'])}</a></td>"
            f"<td>{escape(a['title'])}</td>"
            f"<td>{escape(', '.join((a['applies_to'] or {}).get('repos') or []) or 'project-wide')}</td>"
            f"<td>{escape(a['decision'][:90])}</td>"
            + (f"<td><form method='post' action='/adrs/{escape(a['adr_key'])}/approve'>"
               "<button class='alt'>Approve</button></form></td>" if approvable else "<td></td>")
            + "</tr>"
            for a in items
        )
        return (f"<h2>{escape(title)}</h2><table><tr><th>Key</th><th>Title</th>"
                f"<th>Scope</th><th>Rule</th><th></th></tr>{rows}</table>")

    by = lambda s: [a for a in adrs if a["status"] == s]  # noqa: E731
    body = (
        "<h1>ADR governance rules</h1>"
        + section("Proposed (awaiting your approval)", by("proposed"), True)
        + section("Accepted (live)", by("accepted"), False)
        + section("Superseded / deprecated",
                  by("superseded") + by("deprecated"), False)
        or "<p class='muted'>No ADRs yet.</p>"
    )
    return page("ADRs", body)


def adr_detail(adr: dict[str, Any], incoming: list[str]) -> str:
    def links(keys: list[str]) -> str:
        return ", ".join(f"<a href='/adrs/{escape(k)}'>{escape(k)}</a>"
                         for k in keys) or "<span class='muted'>—</span>"

    sel = adr["applies_to"] or {}
    approve = ""
    if adr["status"] == "proposed":
        approve = (f"<form method='post' action='/adrs/{escape(adr['adr_key'])}/approve'>"
                   "<button class='alt'>Approve (goes live next tick)</button></form>")
    return page(adr["adr_key"], (
        f"<h1>{escape(adr['adr_key'])}: {escape(adr['title'])} "
        f"{_state(adr['status'])}</h1>{approve}"
        f"<h2>Rule (what agents receive)</h2><pre>{escape(adr['decision'])}</pre>"
        f"<h2>Rationale (humans only)</h2><p>{escape(adr['context'] or '—')}</p>"
        "<h2>Scope</h2><p class='muted'>"
        f"work_types: {escape(', '.join(sel.get('work_types') or []) or 'all')} · "
        f"teams: {escape(', '.join(sel.get('teams') or []) or 'all')} · "
        f"repos: {escape(', '.join(sel.get('repos') or []) or 'project-wide')}</p>"
        "<h2>Links</h2>"
        f"<p>related: {links(adr['related'] or [])}<br>"
        f"supersedes: {links(adr['supersedes'] or [])}<br>"
        f"patterns: {escape(', '.join(adr['patterns'] or []) or '—')}<br>"
        f"linked from: {links(incoming)}</p>"
        f"<p class='muted'>proposed by {escape(adr['proposed_by'])}</p>"
    ))


def agents_page(agents: list[dict[str, Any]]) -> str:
    def _seen(a: dict[str, Any]) -> str:
        if a.get("stale"):
            return f"<span class='s-failed'>stale ({escape(str(a.get('last_seen') or '')[:19])})</span>"
        return escape(str(a.get("last_seen") or "never")[:19])

    rows = "".join(
        f"<tr><td>#{a['id']}</td><td>{escape(a['team'])}/{escape(a['function'])}</td>"
        f"<td><span class='pill'>{escape(a['status'])}</span></td>"
        f"<td>{escape(a['runtime'])}</td><td>{_seen(a)}</td></tr>"
        for a in agents
    )
    return page("Agents", (
        "<h1>Agent registry</h1>"
        "<table><tr><th>ID</th><th>Team/Function</th><th>Status</th><th>Runtime</th>"
        f"<th>Last seen</th></tr>{rows}</table>"
        if rows else "<h1>Agent registry</h1><p class='muted'>None registered.</p>"
    ))
