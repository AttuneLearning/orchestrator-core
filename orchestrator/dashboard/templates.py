"""Server-rendered HTML for the ops dashboard.

Plain-Python rendering (html.escape + f-strings) — no template-engine dependency,
matching the orchestrator's minimal-deps posture. Each function returns an HTML
string; page() wraps a body fragment in the shared shell.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional
from urllib.parse import quote

from . import context

_CSS = """
  :root { --bg:#0f1115; --panel:#1a1d24; --ink:#e6e6e6; --muted:#8a91a0;
          --line:#2a2f3a; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --link:#58a6ff;
          --orange:#db6d28; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; flex:none; }
  .dot.green{background:var(--ok)} .dot.yellow{background:var(--warn)}
  .dot.orange{background:var(--orange)} .dot.red{background:var(--bad)}
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
  .doc { max-width:860px; } .doc h1,.doc h2,.doc h3 { margin:18px 0 8px; }
  .doc code { background:#0b0d11; border:1px solid var(--line); border-radius:4px;
              padding:1px 5px; font-size:13px; }
  .doc pre.doc-code { background:#0b0d11; } .doc pre.doc-code code { border:0; padding:0; }
  .doc blockquote { margin:8px 0; padding:4px 12px; border-left:3px solid var(--line);
                    color:var(--muted); }
  .doc ul,.doc ol { padding-left:22px; } .doc hr { border:0; border-top:1px solid var(--line); }
  .doc table.doc-table { border:1px solid var(--line); margin:12px 0; }
  .doc table.doc-table th { color:var(--ink); background:#0b0d11; border-bottom:2px solid var(--line); }
  .doc table.doc-table td { border-bottom:1px solid var(--line); vertical-align:top; }
  .doc table.doc-table tr:hover td { background:rgba(88,166,255,.06); }
  .ai-panel { max-width:900px; margin-top:20px; padding:12px 14px; background:var(--panel);
              border:1px solid var(--line); border-radius:8px; }
  .ai-panel h2 { margin-top:0; } .ai-panel h3 { font-size:14px; margin:14px 0 6px; }
  .ai-panel textarea { width:100%; min-height:66px; padding:8px; background:var(--bg);
              color:var(--ink); border:1px solid var(--line); border-radius:6px; font:inherit; }
  .settings-form { max-width:980px; }
  .settings-group { background:var(--panel); border:1px solid var(--line);
                    border-radius:8px; padding:12px 14px; margin:0 0 14px; }
  .settings-grid { display:grid; grid-template-columns:minmax(190px, 260px) 1fr;
                   gap:10px 14px; align-items:start; }
  .settings-grid label { color:var(--muted); padding-top:6px; }
  .settings-grid input[type=text], .settings-grid input[type=password],
  .settings-grid input[type=number], .settings-grid select {
      width:100%; background:var(--bg); color:var(--ink); border:1px solid var(--line);
      border-radius:6px; padding:6px 8px; font:inherit; }
  .settings-grid .help { color:var(--muted); font-size:12px; margin-top:3px; }
  .settings-grid .env { color:var(--warn); font-size:12px; margin-top:3px; }
  .settings-actions { display:flex; gap:10px; align-items:center; margin:14px 0; }
  .settings-scope { display:flex; gap:8px; align-items:center; margin:0 0 14px; flex-wrap:wrap; }
  .settings-scope a { border:1px solid var(--line); border-radius:6px; padding:5px 10px; }
  .settings-scope a.active { background:var(--link); color:#06101f; border-color:var(--link); }
  .doc-editor { width:100%; max-width:900px; min-height:60vh; padding:8px; background:var(--panel);
              color:var(--ink); border:1px solid var(--line); border-radius:6px;
              font:13px/1.5 ui-monospace,Menlo,monospace; }
  pre.diff { max-height:52vh; overflow:auto; }
  pre.diff span { display:block; }
  .diff-add { color:var(--ok); } .diff-del { color:var(--bad); }
  .diff-ctx { color:var(--muted); } .diff-skip { color:var(--link); font-style:italic; }
"""


def _state(s: str) -> str:
    return f'<span class="pill s-{escape(s)}">{escape(s)}</span>'


_STATUS_DOT = {"live": "🟢", "idle": "🟡", "down": "🔴"}


def _coordinator_picker() -> str:
    """Coordinator switcher (?project=). JS-FREE: a GET form (no action → submits to
    the current page) with a real submit button, so it works even when inline JS is
    blocked/disabled. onchange auto-submits when JS is available (progressive
    enhancement); the Switch button is the guaranteed path otherwise. Hidden when
    only one coordinator exists."""
    if not context.show_picker():
        return ""
    opts = context.instance_options()
    options = "".join(
        f"<option value='{escape(o['key'])}'{' selected' if o['current'] else ''}>"
        f"{_STATUS_DOT.get(o['status'], '')} {escape(o['label'])}</option>"
        for o in opts
    )
    sel_style = ("background:var(--bg);color:var(--ink);border:1px solid var(--line);"
                 "border-radius:5px;padding:4px 8px;font:inherit")
    btn_style = ("background:var(--panel);color:var(--ink);border:1px solid var(--line);"
                 "border-radius:5px;padding:4px 10px;font:inherit;cursor:pointer")
    # No `action` → the GET submits to the CURRENT path, so switching keeps the page
    # and just sets ?project=. `name=project` is what the middleware reads.
    return (
        "<form method='get' style='margin-left:auto;display:flex;gap:6px;align-items:center'>"
        f"<select name='project' title='Coordinator' onchange='this.form.submit()' "
        f"style='{sel_style}'>{options}</select>"
        f"<button type='submit' style='{btn_style}'>Switch</button>"
        "</form>")


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
        "<a href='/workers'>Workers</a>"
        "<a href='/orch/monitor'>Monitor</a>"
        "<a href='/contracts'>Contracts</a>"
        "<a href='/adrs'>ADRs</a>"
        "<a href='/tiers'>Tiers</a>"
        "<a href='/docs'>Docs</a>"
        "<a href='/settings'>Settings</a>"
        "<a href='/api/state'>JSON</a>"
        f"{_coordinator_picker()}"
        "</nav></header><main>"
        f"{body}"
        "</main></body></html>"
    )
    return _with_project(html)


def tiers_page(rows: list[dict[str, Any]]) -> str:
    """GAP-5 / ADR-PROC-003: per (runtime, team) worker performance — the evidence
    for promoting/demoting a model tier on a lane."""
    if not rows:
        body = ("<p>No stamped work events yet — stats accumulate as workers "
                "report through report_work / verify_run / gate_decision.</p>")
    else:
        trs = "".join(
            f"<tr><td>{escape(str(r['team']))}</td><td>{escape(str(r['runtime']))}</td>"
            f"<td>{r['commits']}</td>"
            f"<td>{r['verify_green']}/{r['verifies']}</td>"
            f"<td>{r['avg_verify_s'] if r['avg_verify_s'] is not None else '—'}</td>"
            f"<td>{r['gate_pass']}</td><td>{r['gate_decline']}</td></tr>"
            for r in rows)
        body = ("<table><thead><tr><th>team</th><th>runtime</th><th>commits</th>"
                "<th>verify green</th><th>avg verify s</th><th>gate pass</th>"
                "<th>gate decline</th></tr></thead>"
                f"<tbody>{trs}</tbody></table>"
                "<p style='opacity:.7'>Server-side stamps (unspoofable). Use this to "
                "promote/demote model tiers per lane — ADR-PROC-003.</p>")
    return page("Worker tiers", f"<h1>Worker tiers</h1>{body}")


def workers_page(agents: list[dict[str, Any]], project: str) -> str:
    """Live worker monitor: one panel per registered agent, each tailing that
    worker's tmux pane. Panels auto-refresh in place via polling (no page reload).
    The orchestrator/claude session isn't a registered agent, so it never shows."""
    import json

    def _win(team: str, fn: str) -> str:
        pre = {"backend": "be", "frontend": "fe", "senior": "sr"}.get(team, (team or "wt")[:2])
        return f"{pre}-{fn}"

    panels = []
    for a in sorted(agents, key=lambda x: x["id"]):
        aid, team, fn = a["id"], a.get("team", ""), a.get("function", "")
        status = a.get("status", "?")
        panels.append(
            "<section class='wpanel'>"
            f"<div class='whead'><span class='wdot' id='dot-{aid}' data-status='{escape(status)}'></span>"
            f"<strong>agent {aid} · {escape(team)}/{escape(fn)}</strong>"
            "<span class='muted' style='margin-left:auto'>tmux "
            f"<code>{escape(_win(team, fn))}</code> · <span id='wstat-{aid}'>{escape(status)}</span></span>"
            f"</div><pre class='wlog' id='pane-{aid}'>loading…</pre></section>"
        )
    # Orchestrator/hypervisor session (this Claude session + its subagents) — pinned first.
    # Not a registered agent; its live output comes from the tmux 'orch' window.
    orch_panel = (
        "<section class='wpanel' style='border-color:#3b82f6'>"
        "<div class='whead' style='background:#0e1b2e'>"
        "<span class='wdot' id='dot-orch' style='background:#3b82f6'></span>"
        "<strong>orchestrator · this session (hypervisor + subagents)</strong>"
        "<span class='muted' style='margin-left:auto'>tmux <code>orch</code> · "
        "<span id='wstat-orch'>—</span></span></div>"
        "<pre class='wlog' id='pane-orch'>loading…</pre></section>"
    )
    grid = ("<div class='wgrid'>" + orch_panel + "".join(panels) + "</div>") if (panels or True) else \
        "<p class='muted'>No registered agents to monitor.</p>"

    css = """<style>
      .wtoolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin:8px 0 14px}
      .wgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:14px}
      .wpanel{border:1px solid #2a2f3a;border-radius:8px;overflow:hidden;background:#0d1117}
      .whead{display:flex;align-items:center;gap:8px;padding:7px 10px;background:#161b22;
             border-bottom:1px solid #2a2f3a;font-size:13px;color:#c9d1d9}
      .whead code{color:#9ca3af}
      .wdot{width:9px;height:9px;border-radius:50%;background:#6b7280;flex:none}
      .wdot[data-status=busy]{background:#22c55e}
      .wdot[data-status=idle]{background:#9ca3af}
      .wdot[data-status=offline]{background:#ef4444}
      .wdot.stale{box-shadow:0 0 0 2px #f59e0b}
      .wlog{margin:0;padding:10px;height:340px;overflow:auto;background:#0d1117;color:#c9d1d9;
            font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
      .wlog.err{color:#f59e0b}
    </style>"""

    toolbar = (
        "<div class='wtoolbar'>"
        "<label><input type='checkbox' id='wpause'> pause auto-refresh</label>"
        "<label>scrollback <select id='wlines'>"
        "<option>100</option><option selected>200</option><option>500</option><option>1000</option>"
        "</select> lines</label>"
        "<span class='muted'>updates every 2.5s · last: <span id='wts'>—</span></span>"
        "</div>"
    )

    js = """<script>
      const PROJECT = __PROJECT__;
      function u(p){ return PROJECT ? p+(p.includes('?')?'&':'?')+'project='+encodeURIComponent(PROJECT) : p; }
      let paused=false, lines=200;
      async function refresh(){
        if(paused) return;
        try{
          const r = await fetch(u('/workers/panes?lines='+lines), {cache:'no-store'});
          if(!r.ok) return;
          const data = await r.json();
          for(const id in data){
            const el=document.getElementById('pane-'+id); if(!el) continue;
            const d=data[id];
            const atBottom = el.scrollTop+el.clientHeight >= el.scrollHeight-24;
            el.textContent = d.ok ? (d.text||'(pane empty)') : ('⚠ '+d.text);
            el.classList.toggle('err', !d.ok);
            if(atBottom) el.scrollTop=el.scrollHeight;
            const st=document.getElementById('wstat-'+id); if(st&&d.status) st.textContent=d.status;
            const dot=document.getElementById('dot-'+id);
            if(dot){ if(d.status) dot.dataset.status=d.status; dot.classList.toggle('stale', !!d.stale); }
          }
          const ts=document.getElementById('wts'); if(ts) ts.textContent=new Date().toLocaleTimeString();
        }catch(e){}
      }
      setInterval(refresh, 2500);
      refresh();
      const _p=document.getElementById('wpause');
      if(_p) _p.addEventListener('change',()=>{ paused=_p.checked; if(!paused) refresh(); });
      const _s=document.getElementById('wlines');
      if(_s) _s.addEventListener('change',()=>{ lines=parseInt(_s.value)||200; refresh(); });
    </script>""".replace("__PROJECT__", json.dumps(project or ""))

    body = ("<h1>Workers</h1>"
            "<p class='muted'>Live tail of each registered worker's tmux pane — auto-refreshes "
            "in place, no reload. Panels reflect the current coordinator's agents.</p>"
            + css + toolbar + grid + js)
    return page("Workers", body)


def settings_page(data: dict[str, Any]) -> str:
    """Structured settings editor for global defaults and per-project overrides."""

    def field_html(f: dict[str, Any]) -> str:
        name = f["name"]
        value = f["value"]
        kind = f["kind"]
        choices = f.get("choices") or []
        disabled = ""
        input_name = f"name='{escape(name)}'"
        if kind == "select":
            opts = "".join(
                f"<option value='{escape(str(c))}'"
                f"{' selected' if str(value) == str(c) else ''}>"
                f"{escape(str(c) or '(auto)')}</option>"
                for c in choices
            )
            control = f"<select {input_name}{disabled}>{opts}</select>"
        elif kind == "bool":
            checked = " checked" if bool(value) else ""
            control = (
                f"<input type='hidden' {input_name} value='false'>"
                f"<label style='color:var(--ink);padding-top:0'>"
                f"<input type='checkbox' {input_name} value='true'{checked}{disabled}> enabled"
                "</label>"
            )
        elif kind in ("int", "float"):
            step = "any" if kind == "float" else "1"
            control = (f"<input type='number' step='{step}' {input_name} "
                       f"value='{escape(str(value))}'{disabled}>")
        else:
            typ = "password" if kind == "password" else "text"
            control = (f"<input type='{typ}' {input_name} "
                       f"value='{escape(str(value or ''))}'{disabled}>")
        help_bits = []
        if f.get("help"):
            help_bits.append(f"<div class='help'>{escape(f['help'])}</div>")
        if f.get("env"):
            msg = f"Environment override: {f['env']}"
            if f.get("env_active"):
                msg += (" is currently set, so dashboard edits to this value will not "
                        "take effect until that env var is unset.")
            help_bits.append(f"<div class='env'>{escape(msg)}</div>")
        return (
            f"<label>{escape(f['label'])}<br><code class='muted'>{escape(name)}</code></label>"
            f"<div>{control}{''.join(help_bits)}</div>"
        )

    groups = []
    for group in data["groups"]:
        fields = "".join(field_html(f) for f in group["fields"])
        groups.append(
            "<section class='settings-group'>"
            f"<h2>{escape(group['group'])}</h2>"
            f"<div class='settings-grid'>{fields}</div>"
            "</section>"
        )

    flash = ""
    if data.get("flash"):
        flash = (
            "<div class='banner' style='border-color:var(--ok);color:var(--ok);"
            "background:rgba(63,185,80,.12)'>Settings saved.</div>"
        )
    if data.get("restarted"):
        flash += (
            "<div class='banner' style='border-color:var(--ok);color:var(--ok);"
            "background:rgba(63,185,80,.12)'>Engine restarted. "
            f"New daemon PID: <code>{escape(str(data['restarted']))}</code>.</div>"
        )
    if data.get("restart_error"):
        msg = "Engine restart failed."
        if data.get("restart_error") == "not_configured":
            msg = "Engine restart is only available for configured project instances."
        flash += f"<div class='banner'>{escape(msg)}</div>"
    save_path = escape(data.get("save_path") or data.get("overlay_path") or "")
    scope = data.get("scope") or "project"
    project_label = escape(data.get("project_label") or data.get("project_key") or "")
    project_key = escape(data.get("project_key") or "")
    can_edit_project = bool(data.get("can_edit_project"))
    project_cls = "active" if scope == "project" else ""
    global_cls = "active" if scope == "global" else ""
    project_tab = ""
    if can_edit_project:
        project_tab = (
            f"<a class='{project_cls}' href='/settings?scope=project'>"
            f"Project: {project_label}</a>"
        )
    else:
        project_tab = "<span class='muted'>Project overrides unavailable in single-instance mode</span>"
    scope_note = (
        f"Editing project overrides for <code>{project_key}</code>. These values are stored "
        "under that project&apos;s <code>settings:</code> block in "
        "<code>config/instances.yaml</code> when they differ from the global defaults."
        if scope == "project"
        else "Editing canonical global defaults in <code>config/settings.yaml</code>. "
             "Projects override these defaults in <code>config/instances.yaml</code>."
    )
    restart_panel = ""
    if can_edit_project:
        restart_panel = (
            "<section class='settings-group'>"
            "<h2>Runtime</h2>"
            "<p class='muted'>Restart the selected project&apos;s engine daemon after changing "
            "provider or reasoner settings. This recreates the orchestrator engine and its "
            "reasoner client from the latest config; it does not restart an external "
            "OpenAI-compatible model server.</p>"
            "<form method='post' action='/settings/restart-engine'>"
            f"<input type='hidden' name='scope' value='{escape(scope)}'>"
            "<button class='alt' type='submit' "
            "onclick=\"return confirm('Restart this project engine now?')\">"
            "Restart engine</button>"
            "</form>"
            "</section>"
        )
    body = (
        "<h1>Settings</h1>"
        f"{flash}"
        "<div class='settings-scope'>"
        f"{project_tab}"
        f"<a class='{global_cls}' href='/settings?scope=global'>Global defaults</a>"
        "</div>"
        f"<p class='muted'>{scope_note} Saves write to <code>{save_path}</code>. "
        "Process environment variables still win. Engine daemons and already-created "
        "model clients may need a restart before they pick up provider changes.</p>"
        f"{restart_panel}"
        "<form method='post' action='/settings' class='settings-form'>"
        f"<input type='hidden' name='scope' value='{escape(scope)}'>"
        f"{''.join(groups)}"
        "<div class='settings-actions'><button class='ok' type='submit'>Save settings</button>"
        f"<a href='/settings?scope={escape(scope)}'>Reset form</a></div>"
        "</form>"
    )
    return page("Settings", body)


def docs_page(items: list[dict[str, Any]]) -> str:
    """The Docs tab: shared dev docs from the DB (repo.doc_list rows: path, title,
    format, author, updated_at), grouped by top-level folder of the path slug."""
    new_btn = ("<form method='get' action='/docs/new' style='display:inline'>"
               "<button class='ok' type='submit'>+ New doc</button></form>")
    if not items:
        return page("Docs",
                    "<h1>Docs</h1>"
                    "<p class='muted'>No shared docs yet. Agents write them via the "
                    "<code>doc_write</code> MCP tool; you can create one here too.</p>"
                    f"<p>{new_btn}</p>")

    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        slug = it["path"]
        folder = slug.rsplit("/", 1)[0] if "/" in slug else ""
        groups.setdefault(folder, []).append(it)

    sections = []
    for d in sorted(groups, key=lambda x: (x == "", x)):
        rows = []
        for it in sorted(groups[d], key=lambda x: x["path"]):
            href = f"/docs/view?path={quote(it['path'])}"
            name = it["title"] or it["path"].rsplit("/", 1)[-1]
            when = str(it.get("updated_at") or "")[:16].replace("T", " ")
            rows.append(
                f"<tr><td><a href='{href}'>{escape(name)}</a>"
                f"<div class='muted' style='font-size:12px'>{escape(it['path'])}</div></td>"
                f"<td class='muted'>{escape(it.get('format') or '')}</td>"
                f"<td class='muted'>{escape(it.get('author') or '—')}</td>"
                f"<td class='muted'>{escape(when)}</td></tr>"
            )
        sections.append(
            f"<h2>{escape(d + '/') if d else '(top level)'}</h2>"
            "<table><tr><th>Document</th><th>Format</th><th>Author</th>"
            f"<th>Updated</th></tr>{''.join(rows)}</table>"
        )
    body = (
        "<h1>Docs</h1>"
        f"<p class='muted'>Shared development docs (orchestrator DB) · {len(items)} "
        f"doc(s). Agents read/write via <code>doc_*</code> MCP tools.</p>"
        f"<p>{new_btn}</p>"
        + "".join(sections)
    )
    return page("Docs", body)


def doc_view_page(doc: dict[str, Any], body_html: str) -> str:
    """Single-doc viewer that toggles between a rendered view and an inline
    raw-markdown editor, plus an AI-edit panel (prompt → diff preview → accept).
    HTML docs render in a sandboxed iframe; markdown/text render inline as
    `body_html`. Progressive: the separate /docs/edit page still works if JS is off."""
    path = doc["path"]
    title = doc["title"] or path.rsplit("/", 1)[-1]
    fmt = doc.get("format", "markdown")
    author = doc.get("author") or "human"
    body = doc.get("body") or ""

    meta = (f"<p class='muted'><a href='/docs'>Docs</a> / {escape(path)} · {escape(fmt)}"
            f" · {escape(str(author))}"
            f" · updated {escape(str(doc.get('updated_at') or '')[:16].replace('T',' '))}</p>")

    # Rendered (view) content.
    if fmt == "html":
        srcdoc = escape(body, quote=True)
        rendered = (f"<iframe srcdoc='{srcdoc}' sandbox='allow-same-origin' "
                    "style='width:100%;height:74vh;border:1px solid var(--line);"
                    "border-radius:6px;background:#fff'></iframe>")
    else:
        rendered = f"<div class='doc'>{body_html}</div>"

    # View-mode toolbar.
    view_actions = (
        "<button class='ok' type='button' onclick='__docToggle(true)'>Edit</button> "
        f"<form method='post' action='/docs/delete' style='display:inline' "
        "onsubmit=\"return confirm('Delete this doc?')\">"
        f"<input type='hidden' name='path' value='{escape(path)}'>"
        "<button type='submit'>Delete</button></form>"
    )
    view = (f"<div id='doc-view'><p>{view_actions}</p>{rendered}</div>")

    # Edit mode: raw editor (Save form) + AI-edit panel.
    save_form = (
        "<form method='post' action='/docs/save'>"
        f"<input type='hidden' name='path' value='{escape(path)}'>"
        f"<input type='hidden' name='title' value='{escape(title)}'>"
        f"<input type='hidden' name='format' value='{escape(fmt)}'>"
        f"<input type='hidden' name='author' value='{escape(str(author))}'>"
        f"<textarea id='doc-body' name='body' class='doc-editor'>{escape(body)}</textarea>"
        "<p><button class='ok' type='submit'>Save</button> "
        "<button type='button' onclick='__docToggle(false)'>Cancel</button></p>"
        "</form>"
    )
    ai_panel = (
        "<div class='ai-panel'>"
        "<h2>AI edit</h2>"
        "<p class='muted'>Describe a change to apply to the whole document. "
        "You'll see a diff to accept or discard before anything is saved.</p>"
        "<textarea id='ai-prompt' placeholder='e.g. Tighten the overview and turn "
        "the risks section into a table'></textarea>"
        "<p><button id='ai-go' class='alt' type='button' onclick='__aiEdit()'>"
        "Apply AI edit</button> <span id='ai-status' class='muted'></span></p>"
        "<div id='ai-diff' style='display:none'>"
        "<h3>Proposed changes (previous → new)</h3>"
        "<div id='ai-diff-body'></div>"
        "<p><button class='ok' type='button' onclick='__aiAccept()'>"
        "Accept → load into editor</button> "
        "<button type='button' onclick='__aiDiscard()'>Discard</button></p>"
        "</div></div>"
    )
    edit = (f"<div id='doc-edit' style='display:none'>{save_form}{ai_panel}</div>")

    script = (
        "<script>(function(){"
        f"var PATH={json.dumps(path)};"
        "var proj=new URLSearchParams(location.search).get('project');"
        "function q(u){return proj?(u+(u.indexOf('?')>-1?'&':'?')+'project='+"
        "encodeURIComponent(proj)):u;}"
        "window.__docToggle=function(edit){"
        "document.getElementById('doc-view').style.display=edit?'none':'';"
        "document.getElementById('doc-edit').style.display=edit?'':'none';"
        "if(edit){window.scrollTo(0,0);}};"
        "window.__aiEdit=async function(){"
        "var p=document.getElementById('ai-prompt').value.trim();"
        "var s=document.getElementById('ai-status');"
        "if(!p){s.textContent='Enter an instruction.';return;}"
        "var b=document.getElementById('doc-body').value;"
        "var go=document.getElementById('ai-go');go.disabled=true;"
        "s.textContent='Thinking… (a large document can take a while)';"
        "try{var fd=new URLSearchParams();fd.set('path',PATH);fd.set('prompt',p);"
        "fd.set('body',b);"
        "var r=await fetch(q('/docs/ai-edit'),{method:'POST',headers:{"
        "'Content-Type':'application/x-www-form-urlencoded'},body:fd.toString()});"
        "var j=await r.json();"
        "if(!r.ok){s.textContent=(j&&j.error)||('Error '+r.status);go.disabled=false;return;}"
        "window.__aiNew=j.new_body;"
        "document.getElementById('ai-diff-body').innerHTML=j.diff_html;"
        "document.getElementById('ai-diff').style.display='';"
        "s.textContent=j.changed?'Review the proposed changes below.':"
        "'The AI returned no changes.';"
        "}catch(e){s.textContent='Request failed: '+e;}"
        "go.disabled=false;};"
        "window.__aiAccept=function(){"
        "if(window.__aiNew!=null){document.getElementById('doc-body').value=window.__aiNew;}"
        "document.getElementById('ai-diff').style.display='none';"
        "document.getElementById('ai-status').textContent="
        "'Applied to the editor — review and click Save to persist.';};"
        "window.__aiDiscard=function(){"
        "document.getElementById('ai-diff').style.display='none';"
        "document.getElementById('ai-status').textContent='Discarded.';};"
        "})();</script>"
    )
    return page(title, f"<h1>{escape(title)}</h1>{meta}{view}{edit}{script}")


def doc_edit_page(doc: Optional[dict[str, Any]]) -> str:
    """Create/edit form. `doc` is None for a new doc, else the existing row."""
    is_new = doc is None
    d = doc or {"path": "", "title": "", "body": "", "format": "markdown", "author": ""}
    heading = "New doc" if is_new else f"Edit · {escape(d['path'])}"
    ta = ("width:100%;max-width:900px;min-height:60vh;padding:8px;background:var(--panel);"
          "color:var(--ink);border:1px solid var(--line);border-radius:6px;"
          "font:13px/1.5 ui-monospace,Menlo,monospace")
    inp = ("padding:6px;background:var(--panel);color:var(--ink);"
           "border:1px solid var(--line);border-radius:5px")
    fmt = d.get("format", "markdown")
    opts = "".join(
        f"<option value='{f}'{' selected' if f == fmt else ''}>{f}</option>"
        for f in ("markdown", "html", "text"))
    path_field = (
        f"<input name='path' value='{escape(d['path'])}' placeholder='architecture/my-doc' "
        f"style='{inp};width:420px' required>" if is_new
        else f"<input type='hidden' name='path' value='{escape(d['path'])}'>"
             f"<code>{escape(d['path'])}</code>")
    body = (
        f"<h1>{heading}</h1>"
        "<form method='post' action='/docs/save'>"
        f"<p><label class='muted'>Path </label>{path_field}</p>"
        f"<p><label class='muted'>Title </label>"
        f"<input name='title' value='{escape(d.get('title') or '')}' style='{inp};width:420px'></p>"
        f"<p><label class='muted'>Format </label>"
        f"<select name='format' style='{inp}'>{opts}</select>"
        f"<label class='muted' style='margin-left:14px'>Author </label>"
        f"<input name='author' value='{escape(d.get('author') or 'human')}' style='{inp};width:200px'></p>"
        f"<p><textarea name='body' style='{ta}'>{escape(d.get('body') or '')}</textarea></p>"
        "<p><button class='ok' type='submit'>Save</button> "
        f"<a href='{('/docs' if is_new else '/docs/view?path=' + quote(d['path']))}'>Cancel</a></p>"
        "</form>"
    )
    return page("Edit doc" if not is_new else "New doc", body)


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
            f"<form method='post' action='/orch/monitor/{m['id']}/draft' style='display:inline'>"
            "<button class='alt'>Generate AI draft</button></form>"
            f"<form method='post' action='/orch/monitor/{m['id']}/archive' style='display:inline;margin-left:8px'>"
            "<button class='alt' onclick=\"return confirm('Archive this message?')\">Archive</button></form>"
            "<span class='muted' style='margin-left:8px'>(uses the reasoner — may take a moment)</span>"
            f"<form method='post' action='/orch/monitor/{m['id']}/respond'>"
            "<label class='muted'>Suggested response (agent draft — editable)</label><br>"
            f"<textarea name='suggested' rows='6' style='{_MON_TA}'>{escape(draft)}</textarea><br>"
            "<label class='muted'>Or add your own suggestion (overrides the draft)</label><br>"
            f"<textarea name='override' rows='4' placeholder='leave blank to send the "
            f"draft above' style='{_MON_TA}'></textarea><br>"
            "<button class='ok' style='margin-top:8px'>Submit</button>"
            "</form></div>"
        )
    header = (
        "<h1>Orchestration / Monitor</h1>"
        "<form method='post' action='/orch/monitor/archive-all' style='margin-bottom:12px'>"
        f"<button class='alt' onclick=\"return confirm('Archive all {len(messages)} pending "
        f"message(s)?')\">Archive all ({len(messages)})</button></form>"
    )
    main = header + "".join(blocks)
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
        btns = form("accept", "Accept as agreed", "ok")
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


def _ago(dt: Optional[datetime]) -> str:
    """Compact relative age, e.g. '3m', '2h', '5d'. '—' if unknown."""
    if dt is None:
        return "—"
    try:
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return "—"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


# Human labels for the raw issue_events.event_type values.
_EVENT_LABEL = {
    "state_change": "state change", "gate_enter": "entered gate",
    "gate_pass": "gate passed", "gate_decline": "gate declined",
    "gate_decision": "gate decision", "reclaimed": "reclaimed (stale)",
    "directive": "human directive", "alert": "alert",
    "code_committed": "code committed", "created": "created", "plan": "planned",
    "promoted": "promoted", "promote_conflict": "promote conflict",
    "downstream_synced": "downstream synced", "drift_score": "drift score",
    "error": "error", "cancelled": "cancelled", "decomposed": "decomposed",
    "senior_diagnosis": "senior diagnosis", "note": "note",
    "comms_response_received": "reply received",
}


def _work_status(w: dict[str, Any]) -> tuple[str, str]:
    """(color, label) for a current-work row. Priority: failed > stale > paused >
    active — the most-urgent condition wins so the dot reflects what needs a human."""
    state = w.get("state")
    agent = w.get("agent")
    # failed/off_rails are terminal-ish: the engine will NOT re-drive them and there is
    # no auto-routing to senior — they need a human directive (re-open/retry or cancel).
    # Say that plainly rather than implying senior is already on it.
    if state == "failed":
        return "red", "failed — needs directive (re-open/cancel)"
    if state == "off_rails":
        return "red", "off-rails — needs directive (re-open/cancel)"
    if agent is None:
        return "orange", "stale — unassigned"
    if agent.get("stale"):
        return "orange", "stale — worker silent"
    if w.get("goal_paused"):
        return "yellow", "paused — goal on hold"
    if agent.get("paused_now"):
        return "yellow", "paused — worker cooldown"
    return "green", "active"


_WORK_ORDER = {"red": 0, "orange": 1, "yellow": 2, "green": 3}


def _active_work_panel(work: list[dict[str, Any]]) -> str:
    """The 'Currently being worked on' table: one row per in-progress/latched issue,
    with a status dot, owner, gate, and the most-recent lifecycle event + age."""
    if not work:
        return ("<h2>Currently being worked on</h2>"
                "<p class='muted'>Nothing in progress right now.</p>")
    rows_data = []
    for w in work:
        color, label = _work_status(w)
        rows_data.append((color, label, w))
    rows_data.sort(key=lambda t: (_WORK_ORDER.get(t[0], 9), -(t[2].get("id") or 0)))

    rows = []
    for color, label, w in rows_data:
        agent = w.get("agent")
        if agent is not None:
            owner = (f"{escape(agent.get('team', '') or '')}/"
                     f"{escape(agent.get('function', '') or '')} "
                     f"<span class='muted'>#{agent.get('id')}</span>")
        else:
            owner = "<span class='muted'>unassigned</span>"
        ev = w.get("last_event") or {}
        ev_label = _EVENT_LABEL.get(ev.get("event_type"), ev.get("event_type") or "—")
        to_state = ev.get("to_state")
        if to_state and ev.get("event_type") == "state_change":
            ev_label = f"{ev_label} → {escape(to_state)}"
        last = f"{escape(ev_label)} <span class='muted'>· {_ago(ev.get('created_at'))} ago</span>"
        # A failed/off-rails issue (red) needs a human directive — offer the one-click
        # escalation to the senior dev right on the row.
        if color == "red":
            action = (
                f"<form method='post' action='/issues/{w['id']}/promote-senior' "
                "style='display:inline' "
                "onsubmit=\"return confirm('Re-open and assign to the senior dev?')\">"
                "<button class='alt' title='Re-open and hand to the senior escalation dev'>"
                "→ senior</button></form>"
            )
        else:
            action = ""
        rows.append(
            "<tr>"
            f"<td><span class='dot {color}' title='{escape(label)}'></span></td>"
            f"<td><a href='/issues/{w['id']}'>#{w['id']}</a></td>"
            f"<td>{escape(label)}</td>"
            f"<td>{owner}</td>"
            f"<td class='muted'>{escape(w.get('gate_type') or '—')}</td>"
            f"<td>{last}</td>"
            f"<td>{escape((w.get('title') or '')[:70])}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    return (
        "<h2>Currently being worked on</h2>"
        "<table><tr><th></th><th>Issue</th><th>Status</th><th>Owner</th>"
        "<th>Gate</th><th>Last change</th><th>Title</th><th>Action</th></tr>"
        + "".join(rows) + "</table>"
    )


def _acceptance_panel(acc: Optional[dict[str, Any]]) -> str:
    """E2E acceptance-gate indicator: overall dot + last-run age + per-goal chips.
    The gate is the GLOBAL real-instance Playwright run on merged main
    (e2e-acceptance.sh) — not the per-issue QA gate."""
    if not acc:
        return ""
    goals = acc.get("goals", [])
    accepted = [g for g in goals if g["status"] == "accepted"]
    failed = [g for g in goals if g["status"] == "failed"]
    overall = "red" if failed else ("green" if accepted else "yellow")
    lr = acc.get("last_run")
    when = f"last run {_ago(lr['at'])} ago" if lr and lr.get("at") else "no runs recorded"
    chips = " ".join(
        f"<a href='/goals/{escape(g['goal'])}' style='text-decoration:none;margin-right:6px'>"
        f"<span class='dot {'green' if g['status'] == 'accepted' else 'red'}'></span> "
        f"#{escape(g['goal'])}</a>"
        for g in goals
    ) or "<span class='muted'>no goals evaluated yet</span>"
    line = (f"<p class='muted' style='margin-top:4px'>{escape(lr['line'])}</p>"
            if lr and lr.get("line") else "")
    return (
        f"<h2>E2E acceptance gate <span class='dot {overall}'></span></h2>"
        f"<p class='muted'>{when} · {len(accepted)} accepted / {len(failed)} failed "
        "· global real-instance Playwright gate on merged main</p>"
        f"<p>{chips}</p>{line}"
    )


def _correspondence_main(messages: list[dict[str, Any]], open_count: int) -> str:
    """Correspondence tail rendered in the main column (under the work panel):
    the last N messages between agents / the orchestrator, newest first."""
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
        when = _ago(m.get("created_at"))
        rows.append(
            "<div class='msg'>"
            f"<span class='muted'>{escape(m['from_team'])}→{escape(m['to_team'])} · "
            f"{escape(kind)} · {tag}{escape(reply)}{issue} · {when} ago</span><br>"
            f"{escape((m.get('subject') or '')[:90])}</div>"
        )
    body = "".join(rows) or "<p class='muted'>No correspondence yet.</p>"
    return (f"<h2>Recent correspondence <span class='muted' style='font-weight:400'>"
            f"(last {len(messages)})</span></h2>"
            f"<div style='margin-bottom:10px'>{head}</div>{body}")


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

    # Failed issues need a human directive (the engine won't re-drive terminal state).
    # Re-open (retry) or cancel inline — no need to open each issue.
    # A decomposed parent (epic) has no gate — retrying it is a no-op; the operator
    # must retry its failed child instead. Show a pointer rather than a dead button.
    def _fail_action(i: dict[str, Any]) -> str:
        if i.get("has_children"):
            return ("<span class='muted'>parent epic — "
                    f"<a href='/issues/{i['id']}'>retry its failed child</a></span>")
        return (
            f"<form method='post' action='/issues/{i['id']}/directive' style='display:inline'>"
            "<button class='ok'>re-open / retry</button></form> "
            f"<form method='post' action='/issues/{i['id']}/cancel' style='display:inline' "
            f"onsubmit=\"return confirm('Cancel #{i['id']}? Terminal.')\">"
            "<button>cancel</button></form>"
        )
    fail_rows = "".join(
        f"<tr><td><a href='/issues/{i['id']}'>#{i['id']}</a></td>"
        f"<td class='muted'>{i.get('retry_count', 0)}</td>"
        f"<td>{escape(i['title'])}</td><td>{_fail_action(i)}</td></tr>"
        for i in summary.get("failed_issues", [])
    )
    failed_sec = (
        "<h2>Needs resolution — failed</h2>"
        "<p class='muted'>Terminal (retry cap hit); the engine won't re-drive these. "
        "Re-open to retry, or cancel.</p>"
        "<table><tr><th>Issue</th><th>Retries</th><th>Title</th><th>Action</th></tr>"
        f"{fail_rows}</table>"
        if fail_rows else ""
    )

    def _goal_actions(g: dict[str, Any], *, resume: bool, complete: bool) -> str:
        btns = ""
        if resume:
            btns += (f"<form method='post' action='/goals/{g['id']}/resume'>"
                     "<button class='alt'>Resume</button></form> ")
        if complete:
            btns += (f"<form method='post' action='/goals/{g['id']}/complete'>"
                     "<button class='ok'>Mark complete</button></form>")
        return btns

    def _goal_table(heading: str, items: list[dict[str, Any]], empty: str,
                    *, resume: bool = False, complete: bool = False) -> str:
        actions = resume or complete
        rows = "".join(
            f"<tr><td><a href='/goals/{g['id']}'>#{g['id']}</a></td>"
            f"<td>{_state(g['state'])}</td><td>{escape(g['title'])}</td>"
            f"<td>{g['issue_count']}</td>"
            + (f"<td>{_goal_actions(g, resume=resume, complete=complete)}</td>"
               if actions else "")
            + "</tr>"
            for g in items
        )
        if not rows:
            return f"<h2>{escape(heading)}</h2><p class='muted'>{escape(empty)}</p>"
        action_th = "<th>Action</th>" if actions else ""
        return (f"<h2>{escape(heading)}</h2>"
                "<table><tr><th>Goal</th><th>State</th><th>Title</th>"
                f"<th>Issues</th>{action_th}</tr>{rows}</table>")

    _g = summary["goals_list"]
    _active = [g for g in _g if g["state"] in ("backlog", "planning", "active")]
    _paused = [g for g in _g if g["state"] == "paused"]
    _completed = [g for g in _g if g["state"] == "done"]
    goals = (
        _goal_table("Active goals", _active, "No active goals.", complete=True)
        + _goal_table("Paused or blocked", _paused, "None — nothing held.",
                      resume=True, complete=True)
        + _goal_table("Completed", _completed, "No completed goals yet.")
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
    work_panel = _active_work_panel(summary.get("active_work", []))
    accept_panel = _acceptance_panel(summary.get("acceptance"))
    corr_panel = _correspondence_main(
        summary.get("recent_messages", []), open_count=open_msgs)
    main = (
        f"<h1>Fleet overview · focus {pct}%</h1>{msg_banner}{banner}{flash_html}"
        f"{work_panel}{accept_panel}{corr_panel}"
        f"{add_goal_form}"
        f"<h2>Goals by state</h2>{_counts_cards('goals', summary['goals'])}"
        f"<h2>Issues by state</h2>{_counts_cards('issues', summary['issues'])}"
        f"{suggested_html}{failed_sec}{flagged}{goals}"
    )
    return page("Fleet", main)


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
    if issue["state"] in ("off_rails", "failed"):
        if issue.get("has_children"):
            # A decomposed parent (epic) is a container with no gate of its own —
            # retrying it can't do anything (and used to crash-loop it back to
            # off_rails). Point the operator at the real work: retry the failed child.
            directive = (
                "<p class='muted' style='margin:4px 0'>Parent epic — retry can't apply "
                "(no gate of its own). Re-open the <strong>failed child</strong> issue below; "
                "this epic resolves automatically once its children are healthy.</p>"
            )
        else:
            label = ("Resume (clear quarantine)" if issue["state"] == "off_rails"
                     else "Re-open / retry")
            directive = (
                f"<form method='post' action='/issues/{issue['id']}/directive' style='display:inline'>"
                f"<button class='ok'>{label}</button></form> "
                f"<form method='post' action='/issues/{issue['id']}/promote-senior' "
                "style='display:inline' "
                "onsubmit=\"return confirm('Re-open and assign to the senior dev?')\">"
                "<button class='alt' title='Re-open and hand to the senior escalation dev'>"
                "→ senior</button></form> "
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

    _inp = ("padding:3px 5px;background:var(--bg);color:var(--ink);"
            "border:1px solid var(--line);border-radius:5px;font:inherit")

    def _loop_cell(a: dict[str, Any]) -> str:
        on = bool(a.get("loop_enabled"))
        pill = (f"<span class='pill {'s-done' if on else 's-cancelled'}'>"
                f"{'on' if on else 'off'}</span>")
        toggle = (
            "<form method='post' action='/agents/loop' style='display:inline'>"
            f"<input type='hidden' name='agent_id' value='{a['id']}'>"
            f"<input type='hidden' name='loop_enabled' value='{'false' if on else 'true'}'>"
            f"<button type='submit'>{'disable' if on else 'enable'}</button></form>")
        return f"{pill} {toggle}"

    def _poll_cell(a: dict[str, Any]) -> str:
        iv = int(a.get("poll_interval_seconds") or 300)
        form = (
            "<form method='post' action='/agents/loop' style='display:inline'>"
            f"<input type='hidden' name='agent_id' value='{a['id']}'>"
            f"<input name='poll_interval_seconds' type='number' min='60' max='7200' "
            f"value='{iv}' style='width:64px;{_inp}'> "
            "<button type='submit'>set</button></form>")
        nxt = "—"
        ls = a.get("last_seen")
        if a.get("loop_enabled") and ls is not None:
            try:
                nxt = str(ls + timedelta(seconds=iv))[:19]
            except Exception:  # noqa: BLE001
                nxt = "—"
        return f"{form}<div class='muted' style='font-size:12px'>next ~ {escape(nxt)}</div>"

    def _pause_cell(a: dict[str, Any]) -> str:
        pu = a.get("paused_until")
        active = bool(pu) and pu > datetime.now(timezone.utc)
        label = (f"<span class='s-blocked'>until {escape(str(pu)[:16])}</span>"
                 if active else "<span class='muted'>active</span>")
        pause = (
            "<form method='post' action='/agents/pause' style='display:inline'>"
            f"<input type='hidden' name='agent_id' value='{a['id']}'>"
            "<input type='hidden' name='minutes' value='120'>"
            "<button type='submit'>pause 2h</button></form>")
        resume = (
            "<form method='post' action='/agents/pause' style='display:inline'>"
            f"<input type='hidden' name='agent_id' value='{a['id']}'>"
            "<input type='hidden' name='clear' value='1'>"
            "<button class='ok' type='submit'>resume</button></form>") if active else ""
        return f"{label}<div style='margin-top:2px'>{pause} {resume}</div>"

    rows = "".join(
        f"<tr><td>#{a['id']}</td><td>{escape(a['team'])}/{escape(a['function'])}</td>"
        f"<td><span class='pill'>{escape(a['status'])}</span></td>"
        f"<td>{escape(a['runtime'])}</td><td>{_seen(a)}</td>"
        f"<td>{_loop_cell(a)}</td><td>{_poll_cell(a)}</td><td>{_pause_cell(a)}</td></tr>"
        for a in agents
    )
    registry = (
        "<table><tr><th>ID</th><th>Team/Function</th><th>Status</th><th>Runtime</th>"
        f"<th>Last seen</th><th>Loop</th><th>Poll (s) · next</th><th>Cooldown</th></tr>{rows}</table>"
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
