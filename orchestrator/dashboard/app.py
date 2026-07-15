"""FastAPI app for the ops dashboard.

create_app(pool, settings) builds the app so tests can inject a pool. All data
access goes through repository.py. The POST routes cover the human review
actions: the slice-B directive functions (repository.apply_directive /
resume_goal) — the only audited way out of the off_rails latch and the
paused-goal state — plus promote/reject for goals externally suggested via MCP.
Read rollups (fleet_summary / agents_with_staleness) are shared with the MCP
status tools via orchestrator.monitoring.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yaml

def _read_acceptance(acceptance_dir: str):
    """Read the e2e-acceptance gate markers (.acceptance/) for the Fleet indicator:
    per-goal accepted/failed markers + the last-run.log summary/mtime. Returns None
    if the dir is absent (e.g. a coordinator with no acceptance harness)."""
    d = Path(acceptance_dir)
    if not d.is_dir():
        return None
    goals, seen = [], set()
    for suffix, status in ((".accepted", "accepted"), (".failed", "failed")):
        for f in d.glob(f"goal-*{suffix}"):
            gid = f.stem.split("-")[-1]
            if gid in seen:
                continue
            seen.add(gid)
            goals.append({"goal": gid, "status": status})
    last_run = None
    last = d / "last-run.log"
    if last.is_file():
        st = last.stat()
        txt = last.read_text(errors="replace")
        line = next((ln for ln in reversed(txt.splitlines()) if ln.strip()), "")
        last_run = {"at": datetime.fromtimestamp(st.st_mtime, timezone.utc), "line": line}
    return {"goals": sorted(goals, key=lambda g: int(g["goal"])), "last_run": last_run}

from fastapi import FastAPI, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import CONFIG_DIR, LOCAL_SETTINGS_FILE, SETTINGS_FILE, Settings, load_settings
from ..db import get_pool
from ..monitoring import agents_with_staleness, fleet_summary
from ..roster import load_roster
from . import templates


_SETTINGS_FIELDS = [
    {
        "group": "Model profiles",
        "fields": [
            ("model_profiles.digitalocean.base_url", "DigitalOcean base URL", "text", None,
             "OpenAI-compatible DigitalOcean inference endpoint."),
            ("model_profiles.digitalocean.model", "DigitalOcean model", "text", None, ""),
            ("model_profiles.digitalocean.api_key", "DigitalOcean API key", "password", None,
             "Optional direct secret. Prefer api_key_env so the config stores only an environment variable name."),
            ("model_profiles.digitalocean.api_key_env", "DigitalOcean API key env", "text", None,
             "Environment variable the launcher reads for the API key."),
            ("model_profiles.digitalocean.wire_api", "DigitalOcean wire API", "select", ["chat", "responses"],
             "DigitalOcean tool-calling currently uses chat completions."),
            ("model_profiles.qwen-local.base_url", "Qwen local base URL", "text", None,
             "OpenAI-compatible local endpoint."),
            ("model_profiles.qwen-local.model", "Qwen local model", "text", None, ""),
            ("model_profiles.qwen-local.api_key", "Qwen local API key", "password", None,
             "Blank is allowed for local servers."),
            ("model_profiles.qwen-local.api_key_env", "Qwen local API key env", "text", None,
             "Optional environment variable for the local endpoint API key."),
            ("model_profiles.openai-custom.base_url", "OpenAI-compatible base URL", "text", None, ""),
            ("model_profiles.openai-custom.model", "OpenAI-compatible model", "text", None, ""),
            ("model_profiles.openai-custom.api_key", "OpenAI-compatible API key", "password", None, ""),
            ("model_profiles.openai-custom.api_key_env", "OpenAI-compatible API key env", "text", None,
             "Optional environment variable for the endpoint API key."),
        ],
    },
    {
        "group": "Orch-Manager Codex",
        "fields": [
            ("orch_manager_codex.profile", "Profile", "text", None,
             "Named profile from model_profiles used when no --inference flag is passed."),
            ("orch_manager_codex.model", "Model override", "text", None,
             "Blank uses the selected profile model."),
            ("orch_manager_codex.reasoning_effort", "Reasoning effort", "select", ["", "minimal", "low", "medium", "high"],
             "Passed to Codex model_reasoning_effort when set."),
        ],
    },
    {
        "group": "Orch-Manager Claude",
        "fields": [
            ("orch_manager_claude.account_label", "Account label", "text", None,
             "Informational label only; Claude auth still comes from the local CLI login."),
            ("orch_manager_claude.model", "Claude model", "text", None,
             "Used for orch-manager Claude launches when CLAUDE_MODEL is not set."),
        ],
    },
    {
        "group": "Engine Reasoner",
        "fields": [
            ("engine_reasoner.profile", "Model profile", "text", None,
             "When set, maps the selected profile onto reasoner=openai."),
            ("engine_reasoner.model", "Profile model override", "text", None,
             "Blank uses the selected profile model."),
            ("reasoner", "Reasoner backend", "select", ["", "stub", "anthropic", "openai", "cli"],
             "Empty selects automatically: Anthropic when configured, otherwise stub."),
            ("reasoning_model", "Default reasoning model", "text", None,
             "Fallback model for Anthropic, OpenAI-compatible, and CLI reasoners."),
            ("reasoner_base_url", "Reasoner OpenAI base URL", "text", None,
             "OpenAI-compatible endpoint for reasoner=openai, for example http://host:8081/v1."),
            ("reasoner_model", "Reasoner OpenAI/CLI model", "text", None,
             "Overrides reasoning_model for reasoner=openai or reasoner=cli."),
            ("reasoner_api_key", "Reasoner API key", "password", None,
             "API key for OpenAI-compatible reasoner endpoints; blank is allowed for local servers."),
            ("reasoner_cli_cmd", "Reasoner CLI command", "text", None,
             "Command template for reasoner=cli. Use {prompt} where the combined prompt should go."),
            ("anthropic_api_key", "Anthropic API key", "password", None,
             "Used by Anthropic reasoner mode and auto mode when present."),
        ],
    },
    {
        "group": "Dev/QA Workers",
        "fields": [
            ("devqa_worker.profile", "Worker model profile", "text", None,
             "Default model profile for dashboard-managed dev/QA worker launches."),
            ("devqa_worker.model", "Worker model override", "text", None,
             "Blank uses the selected profile model."),
            ("devqa_worker.runtime", "Worker runtime", "select", ["", "claude", "codex", "qwen", "qwen-code"],
             "Blank keeps the launcher role default."),
        ],
    },
    {
        "group": "Docs AI editor",
        "fields": [
            ("docs_reasoner", "Docs reasoner backend", "select", ["", "stub", "anthropic", "openai", "cli"],
             "Empty reuses the engine reasoner."),
            ("docs_reasoner_base_url", "Docs OpenAI base URL", "text", None,
             "OpenAI-compatible endpoint for Docs AI-edit."),
            ("docs_reasoner_model", "Docs model", "text", None,
             "Model for Docs AI-edit."),
            ("docs_reasoner_api_key", "Docs API key", "password", None,
             "API key for Docs AI-edit endpoint."),
            ("docs_reasoner_cli_cmd", "Docs CLI command", "text", None,
             "Command template for docs_reasoner=cli."),
        ],
    },
    {
        "group": "Code and embedding providers",
        "fields": [
            ("code_provider", "Code provider", "select", ["stub", "openai", "anthropic"],
             "Provider for legacy API code-worker integrations."),
            ("code_base_url", "Code OpenAI base URL", "text", None, ""),
            ("code_model", "Code model", "text", None, ""),
            ("code_api_key", "Code API key", "password", None, ""),
            ("embed_provider", "Embedding provider", "select", ["stub", "openai", "none"],
             "Provider for semantic memory embeddings."),
            ("embed_base_url", "Embedding OpenAI base URL", "text", None, ""),
            ("embed_model", "Embedding model", "text", None, ""),
            ("embed_api_key", "Embedding API key", "password", None, ""),
        ],
    },
    {
        "group": "Internal workflow settings",
        "fields": [
            ("default_pipeline", "Default pipeline", "text", None,
             "Pipeline used for new dashboard goals when no valid pipeline is selected."),
            ("contract_gate_enabled", "Contract gate enabled", "bool", None,
             "Blocks frontend endpoint work until contracts are agreed/live."),
            ("apply_enabled", "Apply artifacts in QA", "bool", None,
             "When enabled, QA gates may apply worker artifacts in an isolated worktree."),
            ("apply_repo_path", "Apply repo path", "text", None, ""),
            ("verify_cmd", "Verify command", "text", None, ""),
            ("auto_promote_enabled", "Auto-promote completed issues", "bool", None,
             "Locally merges completed issue branches into promote_branch. Never pushes."),
            ("promote_repo_path", "Promote repo path", "text", None, ""),
            ("promote_branch", "Promote branch", "text", None, ""),
            ("auto_rebase_downstream", "Sync downstream worktrees", "bool", None,
             "Fast-forward/merge promote_branch into clean downstream worktrees after promotion."),
            ("docs_path", "Shared docs path", "text", None, ""),
            ("database_backup_enabled", "Database backups enabled", "bool", None, ""),
            ("database_backup_dir", "Database backup dir", "text", None, ""),
        ],
    },
    {
        "group": "Thresholds",
        "fields": [
            ("thresholds.drift_threshold", "Drift threshold", "float", None, ""),
            ("thresholds.retry_cap", "Retry cap", "int", None, ""),
            ("thresholds.step_budget", "Step budget", "int", None, ""),
            ("thresholds.max_depth", "Max subissue depth", "int", None, ""),
            ("thresholds.max_subissues", "Max subissues per decomposition", "int", None, ""),
            ("thresholds.max_issues_per_goal", "Max issues per goal", "int", None, ""),
            ("thresholds.max_children_per_parent", "Max children per parent", "int", None, ""),
            ("thresholds.agent_stale_seconds", "Agent stale seconds", "int", None, ""),
            ("thresholds.reclaim_cap", "Reclaim cap", "int", None, ""),
        ],
    },
]

_SETTINGS_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "reasoning_model": "REASONING_MODEL",
    "reasoner": "REASONER",
    "reasoner_base_url": "REASONER_BASE_URL",
    "reasoner_model": "REASONER_MODEL",
    "reasoner_api_key": "REASONER_API_KEY",
    "reasoner_cli_cmd": "REASONER_CLI_CMD",
    "docs_reasoner": "DOCS_REASONER",
    "docs_reasoner_base_url": "DOCS_REASONER_BASE_URL",
    "docs_reasoner_model": "DOCS_REASONER_MODEL",
    "docs_reasoner_api_key": "DOCS_REASONER_API_KEY",
    "docs_reasoner_cli_cmd": "DOCS_REASONER_CLI_CMD",
    "code_provider": "CODE_PROVIDER",
    "code_base_url": "CODE_BASE_URL",
    "code_model": "CODE_MODEL",
    "code_api_key": "CODE_API_KEY",
    "embed_provider": "EMBED_PROVIDER",
    "embed_base_url": "EMBED_BASE_URL",
    "embed_model": "EMBED_MODEL",
    "embed_api_key": "EMBED_API_KEY",
    "default_pipeline": "DEFAULT_PIPELINE",
    "contract_gate_enabled": "CONTRACT_GATE_ENABLED",
    "apply_enabled": "APPLY_ENABLED",
    "apply_repo_path": "APPLY_REPO_PATH",
    "verify_cmd": "VERIFY_CMD",
    "auto_promote_enabled": "AUTO_PROMOTE_ENABLED",
    "promote_repo_path": "PROMOTE_REPO_PATH",
    "promote_branch": "PROMOTE_BRANCH",
    "auto_rebase_downstream": "AUTO_REBASE_DOWNSTREAM",
    "docs_path": "DOCS_PATH",
    "database_backup_enabled": "ORCH_DB_BACKUP_ENABLED",
    "database_backup_dir": "ORCH_DB_BACKUP_DIR",
    "thresholds.drift_threshold": "DRIFT_THRESHOLD",
    "thresholds.retry_cap": "RETRY_CAP",
    "thresholds.step_budget": "STEP_BUDGET",
    "thresholds.max_depth": "MAX_DEPTH",
    "thresholds.max_subissues": "MAX_SUBISSUES",
    "thresholds.max_issues_per_goal": "MAX_ISSUES_PER_GOAL",
    "thresholds.max_children_per_parent": "MAX_CHILDREN_PER_PARENT",
    "thresholds.agent_stale_seconds": "AGENT_STALE_SECONDS",
    "thresholds.reclaim_cap": "RECLAIM_CAP",
}


def _settings_field_names() -> list[str]:
    return [f[0] for group in _SETTINGS_FIELDS for f in group["fields"]]


def _setting_value(settings: Settings, name: str):
    val = settings
    for part in name.split("."):
        if isinstance(val, dict):
            val = val.get(part, "")
        else:
            val = getattr(val, part)
    return val


def _set_nested(data: dict, dotted: str, value) -> None:
    target = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def _delete_nested(data: dict, dotted: str) -> None:
    target = data
    parts = dotted.split(".")
    parents = []
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            return
        parents.append((target, part))
        target = child
    target.pop(parts[-1], None)
    for parent, part in reversed(parents):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            parent.pop(part, None)


def _coerce_setting(raw: str, kind: str):
    if kind == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def _load_local_settings_overlay() -> dict:
    if not LOCAL_SETTINGS_FILE.exists():
        return {}
    with LOCAL_SETTINGS_FILE.open() as fh:
        return yaml.safe_load(fh) or {}


def _load_instances_config() -> dict:
    path = CONFIG_DIR / "instances.yaml"
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _project_settings_overlay(project_key: str) -> dict:
    data = _load_instances_config()
    spec = ((data.get("instances") or {}).get(project_key) or {})
    return spec.get("settings") or {}


def _write_local_settings_overlay(updates: dict) -> None:
    existing = _load_local_settings_overlay()
    for name, value in updates.items():
        _set_nested(existing, name, value)
    LOCAL_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SETTINGS_FILE.open("w") as fh:
        fh.write(
            "# Dashboard-managed local settings overlay. Values here override\n"
            "# config/settings.yaml; process environment variables still win.\n"
        )
        yaml.safe_dump(existing, fh, sort_keys=False)


def _write_global_settings(updates: dict) -> None:
    existing = {}
    if SETTINGS_FILE.exists():
        with SETTINGS_FILE.open() as fh:
            existing = yaml.safe_load(fh) or {}
    for name, value in updates.items():
        _set_nested(existing, name, value)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w") as fh:
        fh.write(
            "# Orchestrator global defaults. Project-local overrides live in\n"
            "# config/instances.yaml under instances.<project>.settings.\n"
            "# Process environment variables still win at runtime.\n\n"
        )
        yaml.safe_dump(existing, fh, sort_keys=False)


def _write_project_settings_overlay(project_key: str, updates: dict,
                                    global_settings: Settings) -> None:
    path = CONFIG_DIR / "instances.yaml"
    data = _load_instances_config()
    data.setdefault("instances", {})
    spec = data["instances"].setdefault(project_key, {})
    spec.setdefault("label", project_key)
    settings = spec.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        spec["settings"] = settings
    for name, value in updates.items():
        _delete_nested(settings, name)
        if value != _setting_value(global_settings, name):
            _set_nested(settings, name, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write(
            "# Coordinators this dashboard can switch between via ?project=<key>.\n"
            "# Global defaults live in config/settings.yaml. Project-local overrides\n"
            "# live under each instance's settings: block below.\n\n"
        )
        yaml.safe_dump(data, fh, sort_keys=False)


def _engine_daemon_pids(project_key: str) -> list[int]:
    """Find engine daemon PIDs for a configured project instance."""
    r = subprocess.run(["pgrep", "-af", "orchestrator.cli"],
                       capture_output=True, text=True, timeout=6)
    if r.returncode not in (0, 1):
        return []
    pids = []
    needle = f"--instance {project_key}"
    for line in r.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        if (needle in cmd and " run " in f" {cmd} " and " --daemon" in cmd
                and "orchestrator.cli" in cmd):
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            if pid != os.getpid():
                pids.append(pid)
    return pids


def _restart_engine_daemon(project_key: str, interval: int = 5) -> dict:
    """Restart the selected project's engine daemon.

    The engine daemon owns the engine reasoner client, so starting a fresh daemon
    reloads reasoner settings. External OpenAI-compatible servers are not managed
    here.
    """
    old_pids = _engine_daemon_pids(project_key)
    for pid in old_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 8
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in old_pids):
            break
        time.sleep(0.2)
    for pid in old_pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    log_path = CONFIG_DIR.parent / f"{project_key}-engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("ab")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "orchestrator.cli",
            "--instance", project_key,
            "run", "--daemon", "--interval", str(interval),
        ],
        cwd=CONFIG_DIR.parent,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"old_pids": old_pids, "new_pid": proc.pid, "log_path": str(log_path)}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def create_app(pool: Optional[ConnectionPool] = None,
               settings: Optional[Settings] = None,
               reasoner=None) -> FastAPI:
    from . import context
    from .instances import load_registry
    # One dashboard, many coordinators (one per DB). The registry routes each
    # request to the coordinator named by ?project=; an injected pool / no
    # instances.yaml collapses to a single 'default' coordinator (current behavior).
    registry = load_registry(settings=settings, pool=pool)
    context.install_registry(registry)
    base_settings = registry.get(registry.default_key).settings
    # Relocate FastAPI's built-in Swagger/OpenAPI UI off "/docs" so that path is
    # free for the human-facing shared-docs browser (the "Docs" nav tab).
    app = FastAPI(title="orchestrator-dashboard",
                  docs_url="/api/swagger", redoc_url="/api/redoc")

    # Request-scoped proxies — these resolve to whichever coordinator ?project=
    # selected, so the route bodies below stay coordinator-agnostic.
    pool = context.POOL
    settings = context.SETTINGS
    _roster = context.ROSTER

    from ..pipelines import load_pipelines
    from ..engine.loop import MONITOR_TEAMS
    # Drafts the suggested reply on the /orch/monitor page. Built once from the
    # default coordinator's settings; tests inject a stub. make_reasoner picks the
    # configured backend (e.g. Qwen).
    if reasoner is None:
        from ..agents.reasoning import make_reasoner
        reasoner = make_reasoner(base_settings)
    _reasoner = reasoner
    # Dedicated reasoner for the Docs AI-edit panel, isolated from engine decisions.
    # When docs_reasoner is set (e.g. openai→local Ollama), document editing uses it
    # while the engine/monitor keep _reasoner. Empty => reuse _reasoner. The cli
    # backend stays available here for a future DB-capable "hypervisor" editor.
    _docs_reasoner = reasoner
    if getattr(base_settings, "docs_reasoner", ""):
        import dataclasses
        from ..agents.reasoning import make_reasoner as _make_docs_reasoner
        _docs_settings = dataclasses.replace(
            base_settings,
            reasoner=base_settings.docs_reasoner,
            reasoner_base_url=base_settings.docs_reasoner_base_url,
            reasoner_model=base_settings.docs_reasoner_model,
            reasoner_api_key=base_settings.docs_reasoner_api_key,
            reasoner_cli_cmd=base_settings.docs_reasoner_cli_cmd,
        )
        try:
            _docs_reasoner = _make_docs_reasoner(_docs_settings)
        except Exception:  # noqa: BLE001 — bad config shouldn't crash the dashboard
            _docs_reasoner = reasoner
    from ..monitor_kb import retrieve_context

    def _pipelines() -> list:
        return sorted(load_pipelines(settings.pipelines))

    def _refresh_registry_settings() -> None:
        reg = context.registry()
        if reg is None:
            return
        if getattr(reg, "configured", False):
            for key, inst in reg.instances.items():
                try:
                    inst.settings = load_settings(instance=key)
                    inst.roster = load_roster(inst.settings.roster)
                except Exception:
                    # Keep the currently loaded settings if a partial local edit
                    # makes one coordinator invalid.
                    pass
        else:
            try:
                inst = context.current()
                inst.settings = load_settings()
                inst.roster = load_roster(inst.settings.roster)
            except Exception:
                pass

    def _settings_payload(flash: str = "", scope: str = "",
                          restarted: str = "", restart_error: str = "") -> dict:
        project_key = context.current_key() or registry.default_key
        project_label = context.current().label
        can_edit_project = bool(project_key and project_key != "default" and registry.configured)
        if not scope:
            scope = "project" if can_edit_project else "global"
        if scope not in ("global", "project"):
            scope = "project"
        if scope == "project" and not can_edit_project:
            scope = "global"
        if scope == "global":
            active_settings = load_settings()
            overlay = _load_local_settings_overlay()
            overlay_path = str(SETTINGS_FILE)
        else:
            active_settings = settings
            overlay = _project_settings_overlay(project_key)
            overlay_path = str(CONFIG_DIR / "instances.yaml")
        rows = []
        for group in _SETTINGS_FIELDS:
            fields = []
            for name, label, kind, choices, help_text in group["fields"]:
                env = _SETTINGS_ENV.get(name, "")
                env_active = bool(env and os.getenv(env) not in (None, ""))
                fields.append({
                    "name": name,
                    "label": label,
                    "kind": kind,
                    "choices": choices or [],
                    "help": help_text,
                    "value": _setting_value(active_settings, name),
                    "env": env,
                    "env_active": env_active,
                })
            rows.append({"group": group["group"], "fields": fields})
        return {
            "groups": rows,
            "overlay_path": str(LOCAL_SETTINGS_FILE),
            "save_path": overlay_path,
            "overlay": overlay,
            "flash": flash,
            "scope": scope,
            "project_key": project_key,
            "project_label": project_label,
            "can_edit_project": can_edit_project,
            "restarted": restarted,
            "restart_error": restart_error,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(saved: str = "", scope: str = "", restarted: str = "",
                      restart_error: str = "") -> str:
        return templates.settings_page(_settings_payload(
            flash=saved, scope=scope, restarted=restarted,
            restart_error=restart_error,
        ))

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        scope = str(form.get("scope") or "project")
        project_key = context.current_key() or registry.default_key
        can_edit_project = bool(project_key and project_key != "default" and registry.configured)
        if scope not in ("global", "project"):
            scope = "project"
        if scope == "project" and not can_edit_project:
            scope = "global"
        updates = {}
        field_kinds = {
            name: kind
            for group in _SETTINGS_FIELDS
            for name, _label, kind, _choices, _help in group["fields"]
        }
        for name in _settings_field_names():
            if name not in form:
                continue
            vals = form.getlist(name)
            raw = str(vals[-1] if vals else "")
            try:
                updates[name] = _coerce_setting(raw, field_kinds[name])
            except ValueError:
                return HTMLResponse(
                    templates.page(
                        "Settings error",
                        f"<h1>Settings error</h1><p>Invalid value for "
                        f"<code>{templates.escape(name)}</code>: "
                        f"<code>{templates.escape(raw)}</code></p>"
                        "<p><a href='/settings'>Back to settings</a></p>",
                    ),
                    status_code=400,
                )
        if scope == "global":
            _write_global_settings(updates)
        else:
            _write_project_settings_overlay(project_key, updates, load_settings())
        _refresh_registry_settings()
        return RedirectResponse(f"/settings?scope={scope}&saved=1", status_code=303)

    @app.post("/settings/restart-engine")
    def settings_restart_engine(scope: str = Form("project")):
        project_key = context.current_key() or registry.default_key
        can_restart = bool(project_key and project_key != "default" and registry.configured)
        if not can_restart:
            return RedirectResponse(
                f"/settings?scope={quote(scope)}&restart_error=not_configured",
                status_code=303,
            )
        try:
            result = _restart_engine_daemon(project_key)
        except Exception:  # noqa: BLE001
            return RedirectResponse(
                f"/settings?scope={quote(scope)}&restart_error=failed",
                status_code=303,
            )
        return RedirectResponse(
            f"/settings?scope={quote(scope)}&restarted={result['new_pid']}",
            status_code=303,
        )

    @app.middleware("http")
    async def _coordinator_scope(request, call_next):
        key = registry.resolve_key(request.query_params.get("project"))
        token = context.set_current(registry.get(key))
        try:
            response = await call_next(request)
        finally:
            context.reset_current(token)
        # Preserve the coordinator across redirects: a write must return to the
        # same DB its form was submitted against (default stays on clean URLs).
        if response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("location", "")
            if (loc.startswith("/") and "://" not in loc
                    and "project=" not in loc and key != registry.default_key):
                sep = "&" if "?" in loc else "?"
                response.headers["location"] = f"{loc}{sep}project={key}"
        return response

    def _monitor_pending() -> list:
        # Pending questions for any monitor team, resolving aliases (e.g. 'orch-monitor')
        # to the canonical team id so alias-addressed messages still surface.
        out = []
        for m in repo.pending_messages(pool):
            team = _roster.resolve(m["to_team"])
            if team is not None and team.id in MONITOR_TEAMS:
                out.append(m)
        return out

    @app.get("/", response_class=HTMLResponse)
    def overview(added: str = "") -> str:
        summary = fleet_summary(pool, settings)
        summary["suggested_goals"] = [asdict(g) for g in
                                      repo.list_goals_by_state(pool, "suggested")]
        summary["pipelines"] = _pipelines()
        summary["default_pipeline"] = settings.default_pipeline
        # Orchestrator-queue alert badge + recent correspondence tail.
        summary["open_monitor_msgs"] = len(_monitor_pending())
        summary["recent_messages"] = repo.list_messages(pool, limit=20)
        # "Currently being worked on" panel: in-progress + latched issues, each with
        # its owner (+ staleness / cooldown), whether its goal is paused, and its
        # most-recent lifecycle event — so the operator sees live work at a glance.
        work = repo.list_issues(pool, states=["in_progress", "failed", "off_rails"])
        agents_by_id = {a["id"]: a for a in agents_with_staleness(pool)}
        now = datetime.now(timezone.utc)
        for a in agents_by_id.values():
            pu = a.get("paused_until")
            a["paused_now"] = bool(pu and pu > now)
        paused_goal_ids = {g["id"] for g in summary.get("paused_goals", [])}
        latest_ev = repo.latest_issue_events(pool, [i.id for i in work])
        summary["active_work"] = [
            {**asdict(i),
             "agent": agents_by_id.get(i.assigned_agent),
             "goal_paused": i.goal_id in paused_goal_ids,
             "last_event": latest_ev.get(i.id)}
            for i in work
        ]
        # E2E acceptance-gate indicator (per-goal markers + last run).
        summary["acceptance"] = _read_acceptance(
            os.environ.get("ACCEPTANCE_DIR",
                           "/home/adam/github/tendcharting-ws/.acceptance"))
        return templates.overview(summary, flash=added)

    # ---- Worker monitor: live tmux-pane tails, one panel per registered agent ----
    # Reads each worker's output straight from its tmux pane (zero worker changes).
    # The orchestrator/claude session is not a registered agent, so it never appears.
    def _tmux_session() -> str:
        # One tmux session per coordinator, named by the project key (?project=).
        return context.current_key() or registry.default_key

    def _worker_window(team: str, function: str) -> str:
        # Registered agents map to tmux windows by convention: be-dev, be-qa,
        # fe-dev, fe-qa, sr-dev (matches start-agent-sessions.sh window names).
        prefix = {"backend": "be", "frontend": "fe", "senior": "sr"}.get(
            team, (team or "wt")[:2])
        return f"{prefix}-{function}"

    def _capture_pane(session: str, window: str, lines: int) -> tuple[bool, str]:
        if not shutil.which("tmux"):
            return False, "tmux is not available on the dashboard host"
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-t", f"{session}:{window}",
                 "-S", f"-{lines}"],
                capture_output=True, text=True, timeout=6)
            if r.returncode != 0:
                return False, (f"tmux window '{session}:{window}' not found — is the "
                               "worker running in that window?")
            return True, r.stdout
        except Exception as e:  # noqa: BLE001 — a capture hiccup must not 500 the page
            return False, f"capture error: {e}"

    @app.get("/workers", response_class=HTMLResponse)
    def workers_page():
        return templates.workers_page(agents_with_staleness(pool), _tmux_session())

    @app.get("/workers/panes")
    def workers_panes(lines: int = 200):
        lines = max(20, min(int(lines), 2000))  # clamp to tmux scrollback
        session = _tmux_session()
        out = {}
        for a in agents_with_staleness(pool):
            win = _worker_window(a.get("team", ""), a.get("function", ""))
            ok, text = _capture_pane(session, win, lines)
            out[str(a["id"])] = {"window": win, "ok": ok, "text": text,
                                 "status": a.get("status"), "stale": a.get("stale")}
        # Orchestrator/hypervisor session (this Claude session + its subagents, e.g. the
        # conflict-resolvers) — the tmux 'orch' window, which is NOT a registered agent.
        ok, text = _capture_pane(session, "orch", lines)
        out["orch"] = {"window": "orch", "ok": ok, "text": text,
                       "status": "orchestrator", "stale": False}
        return JSONResponse(out)

    @app.post("/goals")
    def add_goal(title: str = Form(...), pipeline: str = Form(""),
                 description: str = Form(""), decompose: str = Form("")):
        from urllib.parse import quote
        title = title.strip()
        if not title:
            return RedirectResponse("/", status_code=303)
        pl = pipeline if pipeline in _pipelines() else settings.default_pipeline
        mode = decompose if decompose in ("single", "full") else None
        repo.create_goal(pool, title, description.strip(), pipeline=pl, decompose=mode)
        return RedirectResponse(f"/?added={quote(title)}", status_code=303)

    @app.get("/goals/{goal_id}", response_class=HTMLResponse)
    def goal_detail(goal_id: int):
        goal = next((g for g in repo.list_open_goals(pool) if g.id == goal_id), None)
        if goal is None:
            # open-goals list excludes done/paused; fall back to a direct lookup
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT id, title, description, state, pipeline, created_at, "
                    "updated_at FROM goals WHERE id = %s", (goal_id,)
                ).fetchone()
            if row is None:
                return HTMLResponse(templates.page("Not found",
                                    f"<h1>No goal #{goal_id}</h1>"), status_code=404)
            from ..models import Goal
            goal = Goal(*row)
        issues = [asdict(i) for i in repo.issue_tree(pool, goal_id)]
        return templates.goal_detail(asdict(goal), issues)

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_detail(issue_id: int):
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            return HTMLResponse(templates.page("Not found",
                                f"<h1>No issue #{issue_id}</h1>"), status_code=404)
        events = [asdict(e) for e in repo.issue_timeline(pool, issue_id)]
        d = asdict(issue)
        # A decomposed parent (epic) is a container with no gate — it can't be
        # retried directly; the template hides the retry button for it.
        d["has_children"] = bool(repo.list_issues(pool, parent_id=issue_id))
        return templates.issue_detail(d, events)

    @app.post("/issues/{issue_id}/cancel")
    def cancel_issue(issue_id: int, reason: str = Form("")):
        try:
            repo.cancel_issue(pool, issue_id, reason=reason.strip(), actor="dashboard")
        except ValueError:
            pass  # already terminal — fall through to the detail page
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.get("/agents", response_class=HTMLResponse)
    def agents() -> str:
        return templates.agents_page(agents_with_staleness(pool),
                                     repo.recent_agent_activity(pool, 10))

    @app.post("/agents/loop")
    def agents_loop(agent_id: int = Form(...),
                    loop_enabled: Optional[str] = Form(None),
                    poll_interval_seconds: Optional[int] = Form(None)):
        """Human control from the Agents page: toggle a pull worker's poll loop on/off
        and/or set its idle poll cadence (set_agent_loop bounds it to 60..7200s)."""
        le: Optional[bool] = None
        if loop_enabled not in (None, ""):
            le = str(loop_enabled).lower() in ("1", "true", "yes", "on")
        try:
            repo.set_agent_loop(pool, agent_id, loop_enabled=le,
                                poll_interval_seconds=poll_interval_seconds or None)
        except Exception:  # noqa: BLE001 — bad interval etc.; just bounce back to the page
            pass
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/pause")
    def agents_pause(agent_id: int = Form(...), minutes: Optional[int] = Form(None),
                     clear: Optional[str] = Form(None)):
        """Human/worker cooldown control: pause an agent for N minutes (default 120,
        the token-limit backoff) or clear it (resume now)."""
        from datetime import datetime, timedelta, timezone
        if clear or (minutes is not None and minutes <= 0):
            repo.set_agent_pause(pool, agent_id, None)
        else:
            m = minutes if (minutes and minutes > 0) else 120
            repo.set_agent_pause(pool, agent_id,
                                 datetime.now(timezone.utc) + timedelta(minutes=m))
        return RedirectResponse("/agents", status_code=303)

    @app.get("/agents/{agent_id}/pause")
    def agent_pause_state(agent_id: int):
        """JSON the pull worker polls to self-pace: seconds left on its cooldown."""
        from datetime import datetime, timezone
        a = repo.get_agent(pool, agent_id)
        pu = getattr(a, "paused_until", None) if a else None
        secs = 0
        if pu is not None:
            secs = max(0, int((pu - datetime.now(timezone.utc)).total_seconds()))
        return JSONResponse({"agent_id": agent_id,
                             "paused_until": pu.isoformat() if pu else None,
                             "pause_seconds": secs})

    @app.get("/tiers", response_class=HTMLResponse)
    def tiers() -> str:
        return templates.tiers_page(repo.worker_tier_stats(pool))

    @app.get("/adrs", response_class=HTMLResponse)
    def adrs() -> str:
        return templates.adrs_page(repo.list_adrs(pool))

    @app.get("/adrs/{adr_key}", response_class=HTMLResponse)
    def adr_detail(adr_key: str):
        adr = repo.get_adr(pool, adr_key)
        if adr is None:
            return HTMLResponse(templates.page("Not found",
                                f"<h1>No ADR {adr_key}</h1>"), status_code=404)
        from ..adr_rules import reverse_links
        incoming = reverse_links(repo.list_adrs(pool)).get(adr_key, [])
        return templates.adr_detail(adr, incoming)

    @app.post("/adrs/{adr_key}/approve")
    def adr_approve(adr_key: str):
        repo.approve_adr(pool, adr_key, actor="dashboard")
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/update")
    def adr_update(adr_key: str, decision: str = Form(...), context: str = Form("")):
        repo.update_adr(pool, adr_key, decision=decision, context=context)
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/deactivate")
    def adr_deactivate(adr_key: str):
        repo.deactivate_adr(pool, adr_key, actor="dashboard")
        return RedirectResponse(f"/adrs/{adr_key}", status_code=303)

    @app.post("/adrs/{adr_key}/delete")
    def adr_delete(adr_key: str):
        repo.delete_adr(pool, adr_key)
        return RedirectResponse("/adrs", status_code=303)

    @app.get("/actions", response_class=HTMLResponse)
    def actions_page() -> str:
        # list_pending_actions("pending") lazily expires overdue rows first, so
        # calling it before the resolved-status lookups picks up any rows that
        # flip to 'expired' on this very request.
        pending = repo.list_pending_actions(pool, status="pending")
        resolved: list = []
        for st in ("approved", "denied", "expired", "executed"):
            resolved.extend(repo.list_pending_actions(pool, status=st))
        resolved.sort(key=lambda a: a["resolved_at"] or a["created_at"], reverse=True)
        return templates.actions_page(pending, resolved[:20])

    @app.post("/actions/{action_id}/approve")
    def action_approve(action_id: int):
        try:
            repo.resolve_pending_action(pool, action_id, "approved", resolved_by="dashboard")
        except ValueError:
            # Row is not pending (e.g., expired or already resolved between page render
            # and button click). Fail closed: redirect back to /actions so the user sees
            # the current state instead of a 500 error.
            pass
        return RedirectResponse("/actions", status_code=303)

    @app.post("/actions/{action_id}/deny")
    def action_deny(action_id: int):
        try:
            repo.resolve_pending_action(pool, action_id, "denied", resolved_by="dashboard")
        except ValueError:
            # Row is not pending (e.g., expired or already resolved between page render
            # and button click). Fail closed: redirect back to /actions so the user sees
            # the current state instead of a 500 error.
            pass
        return RedirectResponse("/actions", status_code=303)

    @app.post("/issues/{issue_id}/directive")
    def directive(issue_id: int):
        repo.apply_directive(pool, issue_id, "resume", note="dashboard", actor="dashboard")
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)

    @app.post("/issues/{issue_id}/promote-senior")
    def promote_senior(issue_id: int):
        # Manual escalation behind the 'needs directive' state: re-open a failed/
        # off_rails issue, reset it to the implementation gate, and hand it to the
        # senior escalation dev (assign to the team=senior/function=dev agent) so it
        # appears in senior's queue. Keeps the issue's original team, so its own
        # team QA still verifies the fix afterwards.
        issue = repo.get_issue(pool, issue_id)
        if issue is not None and issue.state in ("failed", "off_rails"):
            repo.apply_directive(pool, issue_id, "resume",
                                 note="promoted to senior via dashboard", actor="dashboard")
            repo.update_state(pool, issue_id, "in_progress", gate_type="implementation",
                              event_type="state_change",
                              payload={"note": "promoted to senior (escalation)"})
            sr = next((a for a in repo.list_agents(pool, team="senior")
                       if a.function == "dev"), None)
            if sr is not None:
                repo.claim_issue(pool, issue_id, sr.id)
        return RedirectResponse("/", status_code=303)

    @app.post("/goals/{goal_id}/resume")
    def resume_goal(goal_id: int):
        repo.resume_goal(pool, goal_id)
        return RedirectResponse(f"/goals/{goal_id}", status_code=303)

    @app.post("/goals/{goal_id}/complete")
    def complete_goal(goal_id: int):
        # Human verdict: the work is actually done (or the goal is being retired).
        repo.complete_goal(pool, goal_id)
        from ..backup import record_backup
        record_backup(pool, base_settings, reason=f"goal-{goal_id}-manual-completion", goal_id=goal_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/goals/{goal_id}/promote")
    def promote_goal(goal_id: int):
        # Human gate: accept an externally suggested goal into the work queue.
        repo.promote_goal(pool, goal_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/goals/{goal_id}/reject")
    def reject_goal(goal_id: int):
        repo.reject_goal(pool, goal_id)
        return RedirectResponse("/", status_code=303)

    def _draft_for(m: dict) -> str:
        """Reasoner-drafted reply for one monitor message (KB-grounded, best-effort).
        Called ON DEMAND — never during page render — because the reasoner may be a
        slow remote model and would otherwise hang the page."""
        q = f"{m['subject']}\n{m.get('body', '')}"
        ctx = "\n\n".join(retrieve_context(pool, q, limit=8))
        try:
            draft = _reasoner.draft_reply(m, context=ctx)
            review = getattr(_reasoner, "review_reply", None)
            if review is not None:
                draft = review(m, context=ctx, draft=draft)
        except Exception as exc:  # noqa: BLE001 - draft is best-effort
            draft = f"[draft unavailable: {exc}]"
        return draft

    @app.get("/orch/monitor", response_class=HTMLResponse)
    def orch_monitor() -> str:
        # Render immediately. Drafts are generated on demand (button → /draft), NOT
        # here — auto-drafting synchronously on load hung the page whenever the
        # reasoner was a slow/contended remote model.
        messages = _monitor_pending()
        history = repo.list_messages(pool, limit=30)
        return templates.orch_monitor(messages, history=history)

    @app.post("/orch/monitor/{message_id}/draft")
    def orch_draft(message_id: int):
        m = next((x for x in _monitor_pending() if x["id"] == message_id), None)
        if m is not None:
            repo.set_message_draft(pool, message_id, _draft_for(m))
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.post("/orch/monitor/{message_id}/respond")
    def orch_respond(message_id: int, suggested: str = Form(""),
                     override: str = Form("")):
        # Human gate: send the override if provided, else the suggested draft.
        body = override.strip() or suggested.strip()
        if body:
            repo.respond_to_message(pool, message_id, body)
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.post("/orch/monitor/{message_id}/archive")
    def orch_archive(message_id: int):
        # Human dismiss: archive one monitor message (status -> archived) so it drops
        # off the pending queue but stays in history. Mark read too for consistency.
        repo.archive_message(pool, message_id)
        repo.mark_message_read(pool, message_id)
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.post("/orch/monitor/archive-all")
    def orch_archive_all():
        # Bulk dismiss: archive every message currently in the pending monitor queue.
        for m in _monitor_pending():
            repo.archive_message(pool, m["id"])
            repo.mark_message_read(pool, m["id"])
        return RedirectResponse("/orch/monitor", status_code=303)

    @app.get("/contracts", response_class=HTMLResponse)
    def contracts_page() -> str:
        return templates.contracts(repo.contracts_overview(pool))

    # -- Docs: shared dev docs in the DB (doc_* MCP tools + dashboard editor) -- #
    def _doc_body_html(doc: dict) -> str:
        from . import docs as docsmod
        fmt = doc.get("format", "markdown")
        if fmt == "markdown":
            return docsmod.render_markdown(doc.get("body") or "")
        if fmt == "html":
            return ""  # rendered via sandboxed iframe in the template
        return f"<pre>{templates.escape(doc.get('body') or '')}</pre>"

    @app.get("/docs", response_class=HTMLResponse)
    def docs_index() -> str:
        return templates.docs_page(repo.doc_list(pool))

    @app.get("/docs/new", response_class=HTMLResponse)
    def docs_new() -> str:
        return templates.doc_edit_page(None)

    @app.get("/docs/view", response_class=HTMLResponse)
    def docs_view(path: str = ""):
        doc = repo.doc_get(pool, path)
        if doc is None:
            return HTMLResponse(templates.page(
                "Not found", f"<h1>No such doc</h1><p class='muted'>"
                f"{templates.escape(path)}</p>"), status_code=404)
        return templates.doc_view_page(doc, _doc_body_html(doc))

    @app.get("/docs/edit", response_class=HTMLResponse)
    def docs_edit(path: str = ""):
        doc = repo.doc_get(pool, path)
        if doc is None:
            return HTMLResponse(templates.page(
                "Not found", f"<h1>No such doc</h1>"), status_code=404)
        return templates.doc_edit_page(doc)

    @app.post("/docs/save")
    def docs_save(path: str = Form(...), title: str = Form(""),
                  body: str = Form(""), format: str = Form("markdown"),
                  author: str = Form("human")):
        path = path.strip().strip("/")
        if not path:
            return RedirectResponse("/docs/new", status_code=303)
        repo.doc_upsert(pool, path, title=title or path.rsplit("/", 1)[-1],
                        body=body, format=format, author=author)
        return RedirectResponse(f"/docs/view?path={quote(path)}", status_code=303)

    @app.post("/docs/ai-edit")
    def docs_ai_edit(path: str = Form(...), prompt: str = Form(...),
                     body: str = Form("")):
        """Apply an AI edit to a whole doc and return {new_body, diff_html, changed}
        as JSON — the Docs viewer shows the diff and lets the human accept/discard.
        `body` is the current editor contents (so unsaved edits are respected);
        falls back to the stored body when empty."""
        from . import docs as docsmod
        prompt = (prompt or "").strip()
        if not prompt:
            return JSONResponse({"error": "Enter an edit instruction."}, status_code=400)
        doc = repo.doc_get(pool, path)
        fmt = (doc or {}).get("format", "markdown")
        title = (doc or {}).get("title", "") or path.rsplit("/", 1)[-1]
        current = body if body != "" else ((doc or {}).get("body") or "")
        editor = getattr(_docs_reasoner, "edit_document", None)
        if editor is None:
            return JSONResponse(
                {"error": "No AI backend is configured for document editing. Set "
                          "DOCS_REASONER=openai|anthropic|cli (and its base_url/model "
                          "or command) in settings.yaml to enable AI edits."},
                status_code=400)
        try:
            new_body = editor(current, prompt, fmt=fmt, title=title)
        except Exception as e:  # noqa: BLE001 — surface backend errors to the UI
            return JSONResponse({"error": f"AI edit failed: {e}"}, status_code=502)
        return JSONResponse({
            "new_body": new_body,
            "diff_html": docsmod.render_diff(current, new_body),
            "changed": (new_body or "").strip() != (current or "").strip(),
        })

    @app.post("/docs/delete")
    def docs_delete(path: str = Form(...)):
        repo.doc_delete(pool, path)
        return RedirectResponse("/docs", status_code=303)

    def _team_pipeline(team: str) -> str:
        return "pull-fe" if team == "frontend" else "pull-1"

    def _changes_to_work(proposals: list) -> None:
        # Group affected endpoints by team that must act (owner + consumers),
        # one goal per team with a sub-issue per endpoint, on the team's pipeline.
        by_team: dict[str, list] = {}
        for p in proposals:
            ep = f"{p['method']} {p['path']}"
            teams = {p["owner_team"]} | set(repo.consumers_of(pool, p["method"], p["path"]))
            for t in teams:
                by_team.setdefault(t, []).append((ep, p["change_type"]))
        for team, eps in by_team.items():
            pl = _team_pipeline(team)
            goal = repo.create_goal(
                pool, f"[contract] {team}: {len(eps)} contract change(s)",
                description="; ".join(f"{e} ({ct})" for e, ct in eps), pipeline=pl)
            for ep, ct in eps:
                repo.create_issue(pool, goal.id, f"{ct}: {ep}", team=team, pipeline=pl,
                                  description=f"Contract {ct} for {ep}; update {team}.")

    @app.post("/contracts/accept")
    def contract_accept(method: str = Form(...), path: str = Form(...)):
        try:
            repo.accept_contract_review(pool, method, path)
        except ValueError as e:
            return HTMLResponse(
                templates.page("Contract Acceptance Error",
                               f"<h1>Contract Acceptance Error</h1><p>{e}</p>"),
                status_code=400
            )
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/accept_with_issue")
    def contract_accept_with_issue(method: str = Form(...), path: str = Form(...)):
        try:
            p = repo.get_proposal(pool, method, path)
            repo.accept_contract_review(pool, method, path, status="agreed")
            if p:
                _changes_to_work([p])
        except ValueError as e:
            return HTMLResponse(
                templates.page("Contract Acceptance Error",
                               f"<h1>Contract Acceptance Error</h1><p>{e}</p>"),
                status_code=400
            )
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/accept_removal")
    def contract_accept_removal(method: str = Form(...), path: str = Form(...)):
        try:
            p = repo.get_proposal(pool, method, path)
            repo.accept_contract_review(pool, method, path)  # remove -> deprecate
            if p:
                _changes_to_work([p])  # consumer cleanup
        except ValueError as e:
            return HTMLResponse(
                templates.page("Contract Acceptance Error",
                               f"<h1>Contract Acceptance Error</h1><p>{e}</p>"),
                status_code=400
            )
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/mark_redevelopment")
    def contract_mark_redev(method: str = Form(...), path: str = Form(...)):
        try:
            p = repo.get_proposal(pool, method, path)
            repo.reject_proposal(pool, method, path)
            if p:
                _changes_to_work([p])
        except ValueError as e:
            # Handle the case where no pending proposal exists for the given method and path
            # This prevents a 500 Internal Server Error and shows a user-friendly message
            return HTMLResponse(
                templates.page("Contract Acceptance Error",
                               f"<h1>Contract Acceptance Error</h1><p>{e}</p>"),
                status_code=400
            )
        return RedirectResponse("/contracts", status_code=303)

    @app.post("/contracts/create_work")
    def contracts_create_work():
        pending = repo.list_proposals(pool, "pending")
        _changes_to_work(pending)
        # accept add/modify shapes so consumers unblock while work proceeds;
        # leave removals for explicit Accept removal.
        for p in pending:
            if p["change_type"] in ("add", "modify"):
                repo.accept_proposal(pool, p["method"], p["path"], status="agreed")
        return RedirectResponse("/contracts", status_code=303)

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        summary = fleet_summary(pool, settings)
        summary["agents"] = agents_with_staleness(pool)
        summary["suggested_goals"] = [asdict(g) for g in
                                      repo.list_goals_by_state(pool, "suggested")]
        # jsonable_encoder handles datetimes the stdlib json encoder can't.
        return JSONResponse(jsonable_encoder(summary))

    return app
