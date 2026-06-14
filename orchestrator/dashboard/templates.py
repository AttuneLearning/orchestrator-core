"""Server-rendered HTML for the ops dashboard.

Plain-Python rendering (html.escape + f-strings) — no template-engine dependency,
matching the orchestrator's minimal-deps posture. Each function returns an HTML
string; page() wraps a body fragment in the shared shell.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any, Optional

from . import context

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
  .s-blocked { color:var(--warn); } .s-cancelled { color:var(--muted); }
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
  button.ok { background:var(--ok); }
  form { display:inline; }
  .cols { display:flex; gap:20px; align-items:flex-start; }
  .col-main { flex:1; min-width:0; }
  aside.side { flex:0 0 300px; background:var(--panel); border:1px solid var(--line);
               border-radius:8px; padding:12px 14px; position:sticky; top:16px; }
  aside.side h2 { margin-top:0; }
  .msg { border-bottom:1px solid var(--line); padding:6px 0; font-size:12px; }
  .msg:last-child { border-bottom:0; }
  .badge { display:inline-block; min-width:16px; text-align:center; padding:1px 7px;
           border-radius:10px; background:var(--line); color:var(--ink); font-weight:700; }
  .badge.alert { background:var(--bad); color:#fff; }
  .unread { color:var(--warn); } .read { color:var(--muted); }
  details.contract { background:var(--panel); border:1px solid var(--line);
                     border-radius:8px; margin-bottom:10px; padding:6px 12px; max-width:860px; }
  details.contract > summary { cursor:pointer; list-style:none; font-weight:600; padding:2px 0; }
  details.contract > summary::-webkit-details-marker { display:none; }
  details.contract > summary::before { content:'▸'; display:inline-block; width:1.1em;
                                       color:var(--muted); }
  details.contract[open] > summary::before { content:'▾'; }
  details.members { margin-top:8px; }
  details.members > summary { cursor:pointer; color:var(--link); font-size:13px; list-style:none; }
  details.members > summary::-webkit-details-marker { display:none; }
  details.members > summary::before { content:'▸ '; color:var(--muted); }
  details.members[open] > summary::before { content:'▾ '; }
"""


def _state(s: str) -> str:
    return f'<span class="pill s-{escape(s)}">{escape(s)}</span>'


_STATUS_DOT = {"live": "🟢", "idle": "🟡", "down": "🔴"}


def _coordinator_picker() -> str:
    """A dropdown to switch coordinators (?project=). Hidden when only one exists."""
    if not context.show_picker():
        return ""
    opts = context.instance_options()
    options = "".join(
        f"<option value='{escape(o['key'])}'{' selected' if o['current'] else ''}>"
        f"{_STATUS_DOT.get(o['status'], '')} {escape(o['label'])}</option>"
        for o in opts
    )
    js = ("var u=new URL(window.location);u.searchParams.set('project',this.value);"
          "window.location=u.toString();")
    return (f"<select title='Coordinator' onchange=\"{js}\" "
            "style='margin-left:auto;background:var(--bg);color:var(--ink);"
            "border:1px solid var(--line);border-radius:5px;padding:4px 8px;font:inherit'>"
            f"{options}</select>")


def _with_project(html: str) -> str:
    """Thread the active coordinator through every internal link/form so navigation
    and writes stay on the same DB. No-op on the default coordinator (clean URLs)."""
    proj = context.current_key()
    if not proj or proj == context.default_key():
        return html

    def repl(m: "re.Match") -> str:
        attr, url = m.group(1), m.group(2)
        if url.startswith("//") or "project=" in url:
            return m.group(0)
        sep = "&" if "?" in url else "?"
        return f"{attr}='{url}{sep}project={proj}'"

    return re.sub(r"(href|action)='(/[^']*)'", repl, html)


def page(title: str, body: str) -> str:
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)} · orchestrator</title><style>{_CSS}</style></head><body>"
        "<header><nav style='display:flex;align-items:center'>"
        "<a href='/'>Fleet</a><a href='/agents'>Agents</a>"
        "<a href='/orch/monitor'>Monitor</a>"
        "<a href='/contracts'>Contracts</a>"
        "<a href='/adrs'>ADRs</a>"
        "<a href='/api/state'>JSON</a>"
        f"{_coordinator_picker()}"
        "</nav></header><main>"
        f"{body}"
        "</main></body></html>"
    )
    return _with_project(html)


_MON_TA = ("width:100%;max-width:760px;padding:6px;background:var(--panel);"
           "color:var(--ink);border:1px solid var(--line);border-radius:5px")


def _history_panel(messages: list[dict[str, Any]], open_count: int) -> str:
    """Side panel: open-queue alert badge + recent correspondence (all messages,
    each linked to its issue + showing source→dest, kind, status/read, thread)."""
    badge_cls = "badge alert" if open_count else "badge"
    head = (f"<a href='/orch/monitor' style='text-decoration:none'>"
            f"<span class='{badge_cls}'>{open_count}</span> open in queue</a>")
    rows = []
    for m in messages:
        kind = m.get("kind", "request")
        issue = (f" · <a href='/issues/{m['issue_id']}'>#{m['issue_id']}</a>"
                 if m.get("issue_id") else "")
        if kind == "response":
            tag = ("<span class='read'>✓ read</span>" if m.get("read_at")
                   else "<span class='unread'>● unread</span>")
        else:
            tag = escape(m.get("status", ""))
        reply = f" ↳#{m['reply_to']}" if m.get("reply_to") else ""
        rows.append(
            "<div class='msg'>"
            f"<span class='muted'>{escape(m['from_team'])}→{escape(m['to_team'])} · "
            f"{escape(kind)} · {tag}{escape(reply)}{issue}</span><br>"
            f"{escape((m.get('subject') or '')[:64])}</div>"
        )
    body = "".join(rows) or "<p class='muted'>No correspondence yet.</p>"
    return (f"<aside class='side'><h2>Correspondence</h2>"
            f"<div style='margin-bottom:10px'>{head}</div>{body}</aside>")


def orch_monitor(messages: list[dict[str, Any]],
                 history: Optional[list[dict[str, Any]]] = None) -> str:
    """Orchestration-monitor inbox: each queued question with an agent-drafted
    reply (editable) plus an override box; Submit sends one of them to the asker.
    A side panel shows all recent correspondence (history)."""
    side = _history_panel(history or [], open_count=len(messages))
    if not messages:
        main = ("<h1>Orchestration / Monitor</h1>"
                "<p class='muted'>No pending questions. Agents reach this queue by "
                "messaging the <code>orchestration</code> team (alias "
                "<code>orch-monitor</code>/<code>monitor</code>).</p>")
        return page("Monitor",
                    f"<div class='cols'><div class='col-main'>{main}</div>{side}</div>")
    blocks = []
    for m in messages:
        link = (f" · issue <a href='/issues/{m['issue_id']}'>#{m['issue_id']}</a>"
                if m.get("issue_id") else "")
        draft = m.get("draft_response") or ""
        blocks.append(
            "<div class='card' style='display:block;max-width:800px;margin-bottom:16px'>"
            f"<div class='muted'>#{m['id']} · from <b>{escape(m['from_team'])}</b> · "
            f"{escape(m.get('priority','medium'))}{link}</div>"
            f"<h2 style='margin:6px 0'>{escape(m['subject'])}</h2>"
            f"<pre>{escape(m.get('body') or '')}</pre>"
            f"<form method='post' action='/orch/monitor/{m['id']}/respond'>"
            "<label class='muted'>Suggested response (agent draft — editable)</label><br>"
            f"<textarea name='suggested' rows='6' style='{_MON_TA}'>{escape(draft)}</textarea><br>"
            "<label class='muted'>Or add your own suggestion (overrides the draft)</label><br>"
            f"<textarea name='override' rows='4' placeholder='leave blank to send the "
            f"draft above' style='{_MON_TA}'></textarea><br>"
            "<button class='ok' style='margin-top:8px'>Submit</button>"
            "</form></div>"
        )
    main = "<h1>Orchestration / Monitor</h1>" + "".join(blocks)
    return page("Monitor",
                f"<div class='cols'><div class='col-main'>{main}</div>{side}</div>")


_CONTRACT_BADGE = {"up-to-date": "accepted", "awaiting acceptance": "proposed",
                   "drifted": "drifted", "removal pending": "drifted",
                   "missing": "proposed", "deprecated": "accepted"}


def _contract_fields(d: Optional[dict[str, Any]], status_key: str) -> str:
    if not d:
        return "<p class='muted'>— (none)</p>"
    def kv(label, val):
        return (f"<div class='muted' style='font-size:12px'>{escape(label)}</div>"
                f"<div>{escape(str(val if val not in (None, '') else '—'))}</div>")
    def kv_link(label, val):
        # type_ref points into the monorepo's packages/contracts; link to the file.
        if not val:
            return kv(label, None)
        href = f"https://github.com/AttuneLearning/cadencelms/blob/main/{escape(str(val))}"
        return (f"<div class='muted' style='font-size:12px'>{escape(label)}</div>"
                f"<div><a href='{href}' target='_blank' rel='noopener'>{escape(str(val))}</a></div>")
    # Full field set so the contract can actually be reviewed for accuracy.
    return (kv("method", d.get("method")) + kv("path", d.get("path"))
            + kv("request_ref", d.get("request_ref")) + kv("response_dto", d.get("response_dto"))
            + kv("auth", d.get("auth")) + kv("owner_team", d.get("owner_team"))
            + kv("version", d.get("version")) + kv("status", d.get(status_key))
            + kv("source_ref", d.get("source_ref"))
            + kv_link("type_ref", d.get("type_ref"))
            + kv("content_hash", (d.get("content_hash") or "")[:16] or None))


def _contract_card(row: dict[str, Any]) -> str:
    c, p, state = row["contract"], row["proposal"], row["state"]
    ep = f"{escape(row['method'])} {escape(row['path'])}"
    badge = _CONTRACT_BADGE.get(state, "proposed")

    def form(action, label, cls=""):
        return (f"<form method='post' action='/contracts/{action}' style='display:inline'>"
                f"<input type='hidden' name='method' value='{escape(row['method'])}'>"
                f"<input type='hidden' name='path' value='{escape(row['path'])}'>"
                f"<button class='{cls}'>{label}</button></form> ")

    if p and p["change_type"] == "remove":
        btns = form("accept_removal", "Accept removal", "alt") + form("mark_redevelopment", "Mark for redevelopment")
    elif p:
        btns = (form("accept", "Accept", "ok")
                + form("accept_with_issue", "Accept changes &amp; create issue", "ok")
                + form("mark_redevelopment", "Mark for redevelopment"))
    elif c and c["status"] == "proposed":
        btns = form("accept", "Accept", "ok")
    else:
        btns = ""
    right = (_contract_fields(p, "target_status")
             + f"<div class='muted'>change: {escape(p['change_type'])}</div>") if p \
        else "<p class='muted'>no proposed change</p>"
    # Collapsible card: summary = endpoint + badge/state; expanding shows ALL member
    # fields directly (current|proposed) plus the action buttons — no second click.
    return (
        "<details class='contract'>"
        f"<summary>{ep} · <span class='badge'>{escape(badge)}</span> "
        f"<span class='muted'>{escape(state)}</span></summary>"
        "<div class='cols' style='margin-top:8px'>"
        f"<div class='col-main'><h3>Current</h3>{_contract_fields(c, 'status')}</div>"
        f"<div class='col-main'><h3>Proposed</h3>{right}</div></div>"
        f"<div style='margin-top:8px'>{btns}</div>"
        "</details>"
    )


def contracts(overview: list[dict[str, Any]]) -> str:
    """Contract master-data page: per-endpoint current|proposed diff + accept
    actions, with a batch 'create work' side panel and a state summary."""
    from collections import Counter
    counts = Counter(r["state"] for r in overview)
    active = [r for r in overview if r["state"] != "up-to-date"]
    uptodate = [r for r in overview if r["state"] == "up-to-date"]
    cards = "".join(_contract_card(r) for r in active) \
        or "<p class='muted'>No pending contract changes — all accepted contracts are up to date.</p>"
    ut = ""
    if uptodate:
        def _ref(r):
            tr = (r.get("contract") or {}).get("type_ref")
            if not tr:
                return ""
            href = f"https://github.com/AttuneLearning/cadencelms/blob/main/{escape(tr)}"
            return (f" <span class='muted'>→</span> "
                    f"<a href='{href}' target='_blank' rel='noopener'>{escape(tr.split('/')[-1])}</a>")
        ut = ("<h2>Up to date</h2><ul>" + "".join(
            f"<li>{escape(r['method'])} {escape(r['path'])}{_ref(r)}</li>" for r in uptodate) + "</ul>")
    # Global single toggle — opens every card + its Details if any are closed, else
    # collapses all; the label flips to match.
    _toggle_js = (
        "var ds=document.querySelectorAll('details.contract');"
        "var o=Array.prototype.some.call(ds,function(d){return !d.open});"
        "ds.forEach(function(d){d.open=o});this.textContent=o?'Collapse all':'Expand all'"
    )
    controls = (f"<div style='margin:8px 0'><button class='alt' "
                f"onclick=\"{_toggle_js}\">Expand all</button></div>")
    main = f"<h1>Contracts</h1>{controls}{cards}{ut}"
    summary = "".join(f"<div class='msg'>{escape(s)}: <b>{n}</b></div>"
                      for s, n in sorted(counts.items())) or "<p class='muted'>No contracts.</p>"
    side = ("<aside class='side'><h2>Changes</h2>"
            "<form method='post' action='/contracts/create_work'>"
            "<button class='ok'>Create goals &amp; issues from changes</button></form>"
            f"<div style='margin-top:10px'>{summary}</div></aside>")
    return page("Contracts", f"<div class='cols'><div class='col-main'>{main}</div>{side}</div>")


def _counts_cards(label: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"<p class='muted'>No {escape(label)}.</p>"
    cards = "".join(
        f"<div class='card'><div class='n'>{n}</div>"
        f"<div class='l'>{escape(state)}</div></div>"
        for state, n in sorted(counts.items())
    )
    return f"<div class='cards'>{cards}</div>"


def overview(summary: dict[str, Any], flash: str = "") -> str:
    pct = round(summary["fleet_focus"] * 100)
    flash_html = (
        f"<div style='background:var(--ok);color:#08130a;padding:8px 14px;"
        f"border-radius:6px;margin:0 0 12px;font-weight:600'>✓ Added goal "
        f"“{escape(flash)}”</div>" if flash else ""
    )
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

    suggested = summary.get("suggested_goals", [])
    suggested_html = ""
    if suggested:
        srows = "".join(
            f"<tr><td>#{g['id']}</td><td>{escape(g['title'])}</td>"
            f"<td class='muted'>{escape(g.get('suggested_by') or '—')}</td>"
            f"<td class='muted'>{escape((g.get('source') or '')[:80])}</td>"
            f"<td><form method='post' action='/goals/{g['id']}/promote'>"
            "<button class='ok'>Promote</button></form> "
            f"<form method='post' action='/goals/{g['id']}/reject'>"
            "<button>Reject</button></form></td></tr>"
            for g in suggested
        )
        suggested_html = (
            "<h2>Suggested goals (awaiting your review)</h2>"
            "<table><tr><th>Goal</th><th>Title</th><th>Suggested by</th>"
            f"<th>Source</th><th>Action</th></tr>{srows}</table>"
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

    def _goal_table(heading: str, items: list[dict[str, Any]], empty: str) -> str:
        rows = "".join(
            f"<tr><td><a href='/goals/{g['id']}'>#{g['id']}</a></td>"
            f"<td>{_state(g['state'])}</td><td>{escape(g['title'])}</td>"
            f"<td>{g['issue_count']}</td></tr>"
            for g in items
        )
        if not rows:
            return f"<h2>{escape(heading)}</h2><p class='muted'>{escape(empty)}</p>"
        return (f"<h2>{escape(heading)}</h2>"
                "<table><tr><th>Goal</th><th>State</th><th>Title</th>"
                f"<th>Issues</th></tr>{rows}</table>")

    _all_goals = summary["goals_list"]
    _completed = [g for g in _all_goals if g["state"] == "done"]
    _active = [g for g in _all_goals if g["state"] != "done"]
    goals = (
        _goal_table("Active Goals", _active, "No active goals.")
        + _goal_table("Completed Goals", _completed, "No completed goals yet.")
    )

    pls = summary.get("pipelines") or []
    default_pl = summary.get("default_pipeline", "")
    opts = "".join(
        f"<option value='{escape(pl)}'{' selected' if pl == default_pl else ''}>{escape(pl)}</option>"
        for pl in pls
    )
    _field = ("padding:5px;background:var(--panel);color:var(--ink);"
              "border:1px solid var(--line);border-radius:5px")
    add_goal_form = (
        "<h2>Add a goal</h2>"
        "<form method='post' action='/goals'>"
        "<div>"
        f"<input name='title' placeholder='Goal title' required "
        f"style='min-width:340px;margin-right:8px;{_field}'>"
        + (f"<select name='pipeline' style='margin-right:8px;{_field}'>{opts}</select>"
           if opts else "")
        + (f"<select name='decompose' title='decomposition' "
           f"style='margin-right:8px;{_field}'>"
           "<option value=''>auto</option>"
           "<option value='single'>single issue</option>"
           "<option value='full'>full decompose</option></select>")
        + "<button class='ok'>Add goal</button>"
        "</div>"
        f"<textarea name='description' placeholder='Description (optional)' rows='2' "
        f"style='display:block;margin-top:8px;width:100%;max-width:560px;{_field}'></textarea>"
        "</form>"
    )

    open_msgs = summary.get("open_monitor_msgs", 0)
    msg_banner = ""
    if open_msgs:
        msg_banner = (
            f"<div class='banner'>📨 {open_msgs} open message(s) in the orchestrator "
            "queue — <a href='/orch/monitor'>review</a>.</div>"
        )
    side = _history_panel(summary.get("recent_messages", []), open_count=open_msgs)
    main = (
        f"<h1>Fleet overview · focus {pct}%</h1>{msg_banner}{banner}{flash_html}"
        f"{add_goal_form}"
        f"<h2>Goals by state</h2>{_counts_cards('goals', summary['goals'])}"
        f"<h2>Issues by state</h2>{_counts_cards('issues', summary['issues'])}"
        f"{suggested_html}{paused_html}{flagged}{goals}"
    )
    return page("Fleet",
                f"<div class='cols'><div class='col-main'>{main}</div>{side}</div>")


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
    # Cancel (terminal triage) for any non-done, non-cancelled issue — the operator
    # path for garbage / misrouted / superseded work (failed + off_rails included).
    cancel = ""
    if issue["state"] not in ("done", "cancelled"):
        cancel = (
            f"<form method='post' action='/issues/{issue['id']}/cancel' "
            "style='display:inline' "
            "onsubmit=\"return confirm('Cancel this issue? It becomes terminal.')\">"
            "<input name='reason' placeholder='reason (optional)' "
            "style='margin-right:6px'>"
            "<button>Cancel issue</button></form>"
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
        f"{directive}{cancel}"
        f"<p>{escape(issue['description'] or '')}</p>"
        f"<h2>Timeline ({len(events)} events)</h2>{ev_html or '<p class=muted>No events.</p>'}"
    ))


def _adr_actions(a: dict[str, Any]) -> str:
    """Status-aware lifecycle buttons (shared by the list + detail views).

    proposed → Approve (go live) + Trash (delete permanently);
    accepted → Deactivate (back to proposed)."""
    k = escape(a["adr_key"])
    st = a["status"]
    if st == "proposed":
        return (
            f"<form method='post' action='/adrs/{k}/approve' style='display:inline'>"
            "<button class='alt'>Approve</button></form> "
            f"<form method='post' action='/adrs/{k}/delete' style='display:inline' "
            f"onsubmit=\"return confirm('Delete {k} permanently? This cannot be undone.')\">"
            "<button>Trash</button></form>"
        )
    if st == "accepted":
        return (
            f"<form method='post' action='/adrs/{k}/deactivate' style='display:inline' "
            f"onsubmit=\"return confirm('Deactivate {k}? It returns to proposed and stops "
            "reaching agents.')\"><button>Deactivate</button></form>"
        )
    return ""


def adrs_page(adrs: list[dict[str, Any]]) -> str:
    def section(title: str, items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        rows = "".join(
            f"<tr><td><a href='/adrs/{escape(a['adr_key'])}'>{escape(a['adr_key'])}</a></td>"
            f"<td>{escape(a['title'])}</td>"
            f"<td>{escape(', '.join((a['applies_to'] or {}).get('repos') or []) or 'project-wide')}</td>"
            f"<td>{escape(a['decision'][:90])}</td>"
            f"<td>{_adr_actions(a)}</td></tr>"
            for a in items
        )
        return (f"<h2>{escape(title)}</h2><table><tr><th>Key</th><th>Title</th>"
                f"<th>Scope</th><th>Rule</th><th></th></tr>{rows}</table>")

    by = lambda s: [a for a in adrs if a["status"] == s]  # noqa: E731
    body = (
        "<h1>ADR governance rules</h1>"
        + section("Proposed (awaiting your approval)", by("proposed"))
        + section("Accepted (live)", by("accepted"))
        + section("Superseded / deprecated",
                  by("superseded") + by("deprecated"))
        or "<p class='muted'>No ADRs yet.</p>"
    )
    return page("ADRs", body)


def adr_detail(adr: dict[str, Any], incoming: list[str]) -> str:
    def links(keys: list[str]) -> str:
        return ", ".join(f"<a href='/adrs/{escape(k)}'>{escape(k)}</a>"
                         for k in keys) or "<span class='muted'>—</span>"

    sel = adr["applies_to"] or {}
    actions = _adr_actions(adr)
    actions_html = f"<p>{actions}</p>" if actions else ""
    _ta = ("width:100%;max-width:640px;padding:6px;background:var(--panel);"
           "color:var(--ink);border:1px solid var(--line);border-radius:5px")
    edit_form = (
        "<h2>Edit (single source of truth)</h2>"
        f"<form method='post' action='/adrs/{escape(adr['adr_key'])}/update'>"
        "<label class='muted'>Rule / decision (what agents receive)</label><br>"
        f"<textarea name='decision' rows='3' required style='{_ta}'>"
        f"{escape(adr['decision'])}</textarea><br>"
        "<label class='muted'>Rationale / context (humans only)</label><br>"
        f"<textarea name='context' rows='3' style='{_ta};margin-top:4px'>"
        f"{escape(adr['context'] or '')}</textarea><br>"
        "<button class='alt' style='margin-top:6px'>Save</button>"
        "<span class='muted'> — edits the live SoT; regenerate agent docs after.</span>"
        "</form>"
    )
    return page(adr["adr_key"], (
        f"<h1>{escape(adr['adr_key'])}: {escape(adr['title'])} "
        f"{_state(adr['status'])}</h1>{actions_html}"
        f"<h2>Rule (what agents receive)</h2><pre>{escape(adr['decision'])}</pre>"
        f"<h2>Rationale (humans only)</h2><p>{escape(adr['context'] or '—')}</p>"
        f"{edit_form}"
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


def agents_page(agents: list[dict[str, Any]],
                activity: Optional[list[dict[str, Any]]] = None) -> str:
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
    registry = (
        "<table><tr><th>ID</th><th>Team/Function</th><th>Status</th><th>Runtime</th>"
        f"<th>Last seen</th></tr>{rows}</table>"
        if rows else "<p class='muted'>None registered.</p>"
    )

    def _who(e: dict[str, Any]) -> str:
        if e.get("team") and e.get("function"):
            return f"#{e['agent_id']} {escape(e['team'])}/{escape(e['function'])}"
        if e.get("agent_id"):
            return f"#{e['agent_id']}"
        return "<span class='muted'>—</span>"

    act_rows = "".join(
        f"<tr><td>{escape(str(e.get('created_at') or '')[:19])}</td>"
        f"<td>{_who(e)}</td><td>{escape(e['action'])}</td>"
        f"<td><a href='/issues/{e['issue_id']}'>#{e['issue_id']}</a> "
        f"{escape((e.get('issue_title') or '')[:50])}</td></tr>"
        for e in (activity or [])
    )
    activity_section = (
        f"<h2>Recent agent activity (latest {len(activity or [])})</h2>"
        "<table><tr><th>When</th><th>Agent</th><th>Action</th><th>Issue</th></tr>"
        f"{act_rows}</table>"
        if act_rows else
        "<h2>Recent agent activity</h2><p class='muted'>No agent actions yet.</p>"
    )

    return page("Agents", (
        "<h1>Agent registry</h1>" + registry + activity_section
    ))
