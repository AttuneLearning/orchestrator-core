"""Configuration loading.

Loads YAML files under config/, then applies process environment overrides.
Repository .env files are intentionally not auto-loaded; local defaults belong
in config/settings.yaml and config/instances.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
MIGRATIONS_DIR = REPO_ROOT / "migrations"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
LOCAL_SETTINGS_FILE = CONFIG_DIR / "settings.local.yaml"


def _yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


@dataclass
class Thresholds:
    drift_threshold: float = 0.5
    retry_cap: int = 3
    step_budget: int = 25
    max_depth: int = 3
    max_subissues: int = 8
    max_issues_per_goal: int = 30
    max_children_per_parent: int = 5  # refuse to fan one parent wider than this
    # Pull-model liveness: a pull-gate issue whose external worker hasn't been
    # seen (heartbeat/claim) within agent_stale_seconds is reclaimed; after
    # reclaim_cap reclaims the issue is quarantined (off_rails).
    agent_stale_seconds: int = 300
    reclaim_cap: int = 3


@dataclass
class Settings:
    database_url: str = "postgresql://orchestrator@localhost:5432/orchestrator"
    anthropic_api_key: str = ""
    reasoning_model: str = "claude-opus-4-8"

    # Reasoner backend (engine decisions): "" = auto (anthropic if key, else stub)
    # | stub | anthropic | openai | cli. `cli` runs on a local coder CLI (your
    # Claude subscription, no API key); `openai` targets any OpenAI-compatible
    # endpoint (e.g. a locally hosted model).
    reasoner: str = ""
    reasoner_base_url: str = ""   # openai-compatible endpoint for reasoner=openai
    reasoner_model: str = ""      # model name for openai/cli (falls back to reasoning_model)
    reasoner_api_key: str = ""
    reasoner_cli_cmd: str = ""    # reasoner=cli command, e.g. 'claude -p "{prompt}"'
    # Overload resilience for reasoner=openai (see OpenAIReasoner). The call
    # retries the primary model, falls back to reasoner_fallback_model, pauses,
    # and retries the whole path before raising ReasonerExhausted.
    reasoner_fallback_model: str = ""   # second model tried when primary is overloaded
    reasoner_retries: int = 3           # attempts per model per whole-path cycle
    reasoner_backoff_base: float = 1.0  # seconds; exponential backoff base
    reasoner_backoff_max: float = 30.0  # seconds; backoff cap
    reasoner_path_pause_s: float = 60.0 # pause between whole-path cycles
    reasoner_path_cycles: int = 2       # whole-path attempts before giving up
    reasoner_request_timeout_s: float = 60.0  # per-request timeout; a hung/overloaded
                                              # endpoint raises (retryable) instead of blocking

    # Docs AI-edit reasoner (dashboard Docs tab only). Isolated from the engine
    # reasoner above so document editing can run on a local model while engine
    # decisions (decompose/gate/drift) stay on their own backend. Empty
    # docs_reasoner => reuse the engine reasoner. Same values: stub|anthropic|openai|cli.
    docs_reasoner: str = ""
    docs_reasoner_base_url: str = ""
    docs_reasoner_model: str = ""
    docs_reasoner_api_key: str = ""
    docs_reasoner_cli_cmd: str = ""

    code_provider: str = "stub"          # stub | openai | anthropic
    code_base_url: str = ""
    code_model: str = ""
    code_api_key: str = ""

    # Command template for runtime=cli agents. Placeholders: {prompt} {session_id}
    cli_agent_cmd: str = ""

    # Apply/verify leg (slice F). OFF by default: artifacts stay stored-only.
    # When enabled, qa_gate work applies the artifact in an isolated git worktree
    # of apply_repo_path and runs verify_cmd there. Promotion (merge) only ever
    # happens via the explicit human CLI directive `apply-promote`.
    apply_enabled: bool = False
    apply_repo_path: str = ""
    verify_cmd: str = ""

    # Coordinator DB backups. The engine runs this best-effort after e2e gate
    # success and goal completion. The CLI/script can run it manually too.
    database_backup_enabled: bool = True
    database_backup_dir: str = "backups/orchestrator-db"

    # Auto-promote on completion: when an issue passes its final gate, the engine
    # merges that issue's committed branch into promote_branch in a dedicated
    # worktree of promote_repo_path, so the next team sees the work. OFF by default.
    # LOCAL merge only — never pushes (downstream teams pull the integration branch).
    # A merge conflict bounces the issue back to implementation (rebase & resolve)
    # rather than failing silently.
    auto_promote_enabled: bool = False
    promote_repo_path: str = ""
    promote_branch: str = "main"
    # After a successful promote, bring promote_branch into each downstream worktree so
    # the next team sees the integrated work without a manual pull. SAFE by design: only
    # touches CLEAN worktrees, SKIPS active `issue-*` work branches, the main checkout, and
    # the integrator; fast-forwards (or merges) — never rebases/rewrites history — and
    # aborts on conflict, leaving the worktree pristine. OFF by default.
    auto_rebase_downstream: bool = False

    # Shared development docs browser (dashboard "Docs" tab). Read-only: the dashboard
    # lists and renders files under this dir (and subdirs) so agents/humans share dev
    # docs in one place. Empty = tab hidden. Markdown/HTML/text are viewable.
    docs_path: str = ""

    # Contract-first gate (migration 0011). OFF by default so projects without
    # cross-team API contracts pay nothing. When on, pull-fe's contract_check gate
    # blocks new-endpoint frontend issues until their consumed endpoints have an
    # agreed/live contract. Live toggle: CONTRACT_GATE_ENABLED.
    contract_gate_enabled: bool = False

    default_pipeline: str = "pipeline-1"
    thresholds: Thresholds = field(default_factory=Thresholds)

    embed_provider: str = "stub"           # stub | openai | none
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = ""

    # Dashboard-managed model profiles for runtime launchers and role-specific
    # model selection. These complement the legacy reasoner_*, code_*, and
    # embed_* fields rather than replacing them.
    model_profiles: dict[str, Any] = field(default_factory=lambda: {
        "digitalocean": {
            "base_url": "https://inference.do-ai.run/v1",
            "model": "deepseek-v4-pro",
            "api_key": "",
            "api_key_env": "MODEL_ACCESS_KEY",
            "wire_api": "chat",
        },
        "qwen-local": {
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "",
            "wire_api": "chat",
        },
        "openai-custom": {
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "",
            "wire_api": "chat",
        },
    })
    orch_manager_codex: dict[str, Any] = field(default_factory=lambda: {
        "profile": "digitalocean",
        "model": "",
        "reasoning_effort": "high",
    })
    orch_manager_claude: dict[str, Any] = field(default_factory=lambda: {
        "account_label": "",
        "model": "",
    })
    engine_reasoner: dict[str, Any] = field(default_factory=lambda: {
        "profile": "",
        "model": "",
        "fallback_model": "",
    })
    devqa_worker: dict[str, Any] = field(default_factory=lambda: {
        "profile": "",
        "model": "",
        "runtime": "",
    })

    # Raw parsed YAML for the pipeline/roster modules to consume.
    pipelines: dict[str, Any] = field(default_factory=dict)
    roster: dict[str, Any] = field(default_factory=dict)
    # Which roster file is active (relative to repo root). Lets us keep the
    # independent-repos roster and a monorepo roster side by side. Instance
    # config selects the active roster; ROSTER_FILE remains an explicit env
    # override. Default = config/roster.yaml.
    roster_file: str = "config/roster.yaml"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _resolve_instance(instance: str, s_yaml: dict[str, Any]) -> str:
    """Resolve an orchestrator instance (dev group) from config/instances.yaml.

    Returns the instance's database URL and mutates s_yaml in place with the
    instance's roster_file + any `settings:` overrides, so the rest of
    load_settings() (picks/thresholds) sees group-local values.
    `database_url` is the local default. `database_url_env` remains available
    for deployments that inject secrets via the process environment.
    """
    instances = (_yaml(CONFIG_DIR / "instances.yaml") or {}).get("instances", {}) or {}
    entry = instances.get(instance)
    if entry is None:
        raise ValueError(
            f"unknown orchestrator instance {instance!r}; "
            f"known: {sorted(instances)} (see config/instances.yaml)")
    env_name = entry.get("database_url_env")
    db = (os.getenv(env_name) if env_name else None) or entry.get("database_url")
    if not db:
        raise ValueError(
            f"instance {instance!r}: env {env_name!r} is unset and no literal "
            "database_url is given in config/instances.yaml")
    if entry.get("roster_file"):
        s_yaml["roster_file"] = entry["roster_file"]
    # Arbitrary per-group Settings overrides (e.g. reasoner, docs_reasoner,
    # default_pipeline). These override settings.yaml but env still wins (picks).
    for key, val in (entry.get("settings") or {}).items():
        s_yaml[key] = val
    return db


def load_settings(instance: str | None = None) -> Settings:
    """Load settings from config/*.yaml, then apply environment overrides.

    If `instance` (or the ORCH_INSTANCE env var) is set, that instance from
    config/instances.yaml is authoritative for the database + roster + any
    group-local settings overrides."""
    s_yaml = _deep_merge(_yaml(SETTINGS_FILE), _yaml(LOCAL_SETTINGS_FILE))
    instance = instance or os.getenv("ORCH_INSTANCE") or None
    inst_db = _resolve_instance(instance, s_yaml) if instance else None
    t_yaml = s_yaml.get("thresholds", {}) or {}

    thresholds = Thresholds(
        drift_threshold=_env_float("DRIFT_THRESHOLD", t_yaml.get("drift_threshold", 0.5)),
        retry_cap=_env_int("RETRY_CAP", t_yaml.get("retry_cap", 3)),
        step_budget=_env_int("STEP_BUDGET", t_yaml.get("step_budget", 25)),
        max_depth=_env_int("MAX_DEPTH", t_yaml.get("max_depth", 3)),
        max_subissues=_env_int("MAX_SUBISSUES", t_yaml.get("max_subissues", 8)),
        max_issues_per_goal=_env_int("MAX_ISSUES_PER_GOAL", t_yaml.get("max_issues_per_goal", 30)),
        max_children_per_parent=_env_int("MAX_CHILDREN_PER_PARENT", t_yaml.get("max_children_per_parent", 5)),
        agent_stale_seconds=_env_int("AGENT_STALE_SECONDS", t_yaml.get("agent_stale_seconds", 300)),
        reclaim_cap=_env_int("RECLAIM_CAP", t_yaml.get("reclaim_cap", 3)),
    )

    def pick(env: str, yaml_key: str, default: str) -> str:
        val = os.getenv(env)
        if val not in (None, ""):
            return val
        return str(s_yaml.get(yaml_key, default))

    def pick_bool(env: str, yaml_key: str, default: bool = False) -> bool:
        val = os.getenv(env)
        if val not in (None, ""):
            return val.lower() in ("1", "true", "yes")
        return bool(s_yaml.get(yaml_key, default))

    def pick_dict(yaml_key: str, default: dict[str, Any]) -> dict[str, Any]:
        val = s_yaml.get(yaml_key, {})
        if not isinstance(val, dict):
            val = {}
        return _deep_merge(default, val)

    # A selected instance's roster is authoritative over an ambient ROSTER_FILE env
    # (selecting the dev group must pick that group's roster, not the shell default).
    if instance and s_yaml.get("roster_file"):
        roster_file = s_yaml["roster_file"]
    else:
        roster_file = pick("ROSTER_FILE", "roster_file", "config/roster.yaml")

    defaults = Settings()
    model_profiles = pick_dict("model_profiles", defaults.model_profiles)
    orch_manager_codex = pick_dict("orch_manager_codex", defaults.orch_manager_codex)
    orch_manager_claude = pick_dict("orch_manager_claude", defaults.orch_manager_claude)
    engine_reasoner = pick_dict("engine_reasoner", defaults.engine_reasoner)
    devqa_worker = pick_dict("devqa_worker", defaults.devqa_worker)

    reasoner = pick("REASONER", "reasoner", "")
    reasoner_base_url = pick("REASONER_BASE_URL", "reasoner_base_url", "")
    reasoner_model = pick("REASONER_MODEL", "reasoner_model", "")
    reasoner_api_key = pick("REASONER_API_KEY", "reasoner_api_key", "")
    reasoner_fallback_model = pick("REASONER_FALLBACK_MODEL", "reasoner_fallback_model", "")
    profile_name = str(engine_reasoner.get("profile") or "")
    profile = model_profiles.get(profile_name) if profile_name else None
    if isinstance(profile, dict):
        if os.getenv("REASONER") in (None, ""):
            reasoner = "openai"
        if os.getenv("REASONER_BASE_URL") in (None, ""):
            reasoner_base_url = str(profile.get("base_url") or "")
        if os.getenv("REASONER_MODEL") in (None, ""):
            reasoner_model = str(engine_reasoner.get("model") or profile.get("model") or "")
        if os.getenv("REASONER_FALLBACK_MODEL") in (None, "") \
                and engine_reasoner.get("fallback_model"):
            reasoner_fallback_model = str(engine_reasoner["fallback_model"])
        if os.getenv("REASONER_API_KEY") in (None, ""):
            api_key_env = str(profile.get("api_key_env") or "")
            reasoner_api_key = (
                os.getenv(api_key_env, "") if api_key_env else ""
            ) or str(profile.get("api_key") or "")

    return Settings(
        database_url=inst_db or pick("DATABASE_URL", "database_url", Settings.database_url),
        anthropic_api_key=pick("ANTHROPIC_API_KEY", "anthropic_api_key", ""),
        reasoning_model=pick("REASONING_MODEL", "reasoning_model", "claude-opus-4-8"),
        reasoner=reasoner,
        reasoner_base_url=reasoner_base_url,
        reasoner_model=reasoner_model,
        reasoner_api_key=reasoner_api_key,
        reasoner_fallback_model=reasoner_fallback_model,
        reasoner_retries=int(pick("REASONER_RETRIES", "reasoner_retries", "3")),
        reasoner_backoff_base=float(pick("REASONER_BACKOFF_BASE", "reasoner_backoff_base", "1.0")),
        reasoner_backoff_max=float(pick("REASONER_BACKOFF_MAX", "reasoner_backoff_max", "30.0")),
        reasoner_path_pause_s=float(pick("REASONER_PATH_PAUSE_S", "reasoner_path_pause_s", "60.0")),
        reasoner_path_cycles=int(pick("REASONER_PATH_CYCLES", "reasoner_path_cycles", "2")),
        reasoner_request_timeout_s=float(pick("REASONER_REQUEST_TIMEOUT_S", "reasoner_request_timeout_s", "60.0")),
        reasoner_cli_cmd=pick("REASONER_CLI_CMD", "reasoner_cli_cmd", ""),
        docs_reasoner=pick("DOCS_REASONER", "docs_reasoner", ""),
        docs_reasoner_base_url=pick("DOCS_REASONER_BASE_URL", "docs_reasoner_base_url", ""),
        docs_reasoner_model=pick("DOCS_REASONER_MODEL", "docs_reasoner_model", ""),
        docs_reasoner_api_key=pick("DOCS_REASONER_API_KEY", "docs_reasoner_api_key", ""),
        docs_reasoner_cli_cmd=pick("DOCS_REASONER_CLI_CMD", "docs_reasoner_cli_cmd", ""),
        code_provider=pick("CODE_PROVIDER", "code_provider", "stub"),
        code_base_url=pick("CODE_BASE_URL", "code_base_url", ""),
        code_model=pick("CODE_MODEL", "code_model", ""),
        code_api_key=pick("CODE_API_KEY", "code_api_key", ""),
        cli_agent_cmd=pick("CLI_AGENT_CMD", "cli_agent_cmd", ""),
        apply_enabled=pick_bool("APPLY_ENABLED", "apply_enabled", False),
        apply_repo_path=pick("APPLY_REPO_PATH", "apply_repo_path", ""),
        verify_cmd=pick("VERIFY_CMD", "verify_cmd", ""),
        database_backup_enabled=pick_bool("ORCH_DB_BACKUP_ENABLED", "database_backup_enabled", True),
        database_backup_dir=pick("ORCH_DB_BACKUP_DIR", "database_backup_dir", "backups/orchestrator-db"),
        auto_promote_enabled=pick_bool("AUTO_PROMOTE_ENABLED", "auto_promote_enabled", False),
        promote_repo_path=pick("PROMOTE_REPO_PATH", "promote_repo_path", ""),
        promote_branch=pick("PROMOTE_BRANCH", "promote_branch", "main"),
        auto_rebase_downstream=pick_bool("AUTO_REBASE_DOWNSTREAM", "auto_rebase_downstream", False),
        docs_path=pick("DOCS_PATH", "docs_path", ""),
        contract_gate_enabled=pick_bool("CONTRACT_GATE_ENABLED", "contract_gate_enabled", False),
        default_pipeline=pick("DEFAULT_PIPELINE", "default_pipeline", "pipeline-1"),
        thresholds=thresholds,
        pipelines=_yaml(CONFIG_DIR / "pipelines.yaml"),
        roster=_yaml(REPO_ROOT / roster_file),
        roster_file=roster_file,
        embed_provider=pick("EMBED_PROVIDER", "embed_provider", "stub"),
        embed_base_url=pick("EMBED_BASE_URL", "embed_base_url", ""),
        embed_model=pick("EMBED_MODEL", "embed_model", ""),
        embed_api_key=pick("EMBED_API_KEY", "embed_api_key", ""),
        model_profiles=model_profiles,
        orch_manager_codex=orch_manager_codex,
        orch_manager_claude=orch_manager_claude,
        engine_reasoner=engine_reasoner,
        devqa_worker=devqa_worker,
    )
