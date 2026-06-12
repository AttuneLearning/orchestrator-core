"""Configuration loading.

Loads .env (via python-dotenv) and the three YAML files under config/, merging
them into a single typed Settings object. Environment variables always override
YAML values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Thresholds:
    drift_threshold: float = 0.5
    retry_cap: int = 3
    step_budget: int = 25
    max_depth: int = 3
    max_subissues: int = 8
    max_issues_per_goal: int = 30


@dataclass
class Settings:
    database_url: str = "postgresql://orchestrator@localhost:5432/orchestrator"
    anthropic_api_key: str = ""
    reasoning_model: str = "claude-opus-4-8"

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

    default_pipeline: str = "pipeline-1"
    thresholds: Thresholds = field(default_factory=Thresholds)

    embed_provider: str = "stub"           # stub | openai | none
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = ""

    # Raw parsed YAML for the pipeline/roster modules to consume.
    pipelines: dict[str, Any] = field(default_factory=dict)
    roster: dict[str, Any] = field(default_factory=dict)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def load_settings() -> Settings:
    """Load settings from config/*.yaml, then apply environment overrides."""
    load_dotenv(REPO_ROOT / ".env")

    s_yaml = _yaml(CONFIG_DIR / "settings.yaml")
    t_yaml = s_yaml.get("thresholds", {}) or {}

    thresholds = Thresholds(
        drift_threshold=_env_float("DRIFT_THRESHOLD", t_yaml.get("drift_threshold", 0.5)),
        retry_cap=_env_int("RETRY_CAP", t_yaml.get("retry_cap", 3)),
        step_budget=_env_int("STEP_BUDGET", t_yaml.get("step_budget", 25)),
        max_depth=_env_int("MAX_DEPTH", t_yaml.get("max_depth", 3)),
        max_subissues=_env_int("MAX_SUBISSUES", t_yaml.get("max_subissues", 8)),
        max_issues_per_goal=_env_int("MAX_ISSUES_PER_GOAL", t_yaml.get("max_issues_per_goal", 30)),
    )

    def pick(env: str, yaml_key: str, default: str) -> str:
        val = os.getenv(env)
        if val not in (None, ""):
            return val
        return str(s_yaml.get(yaml_key, default))

    return Settings(
        database_url=os.getenv("DATABASE_URL", Settings.database_url),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        reasoning_model=pick("REASONING_MODEL", "reasoning_model", "claude-opus-4-8"),
        code_provider=pick("CODE_PROVIDER", "code_provider", "stub"),
        code_base_url=pick("CODE_BASE_URL", "code_base_url", ""),
        code_model=pick("CODE_MODEL", "code_model", ""),
        code_api_key=os.getenv("CODE_API_KEY", ""),
        cli_agent_cmd=pick("CLI_AGENT_CMD", "cli_agent_cmd", ""),
        apply_enabled=os.getenv("APPLY_ENABLED", "").lower() in ("1", "true", "yes"),
        apply_repo_path=pick("APPLY_REPO_PATH", "apply_repo_path", ""),
        verify_cmd=pick("VERIFY_CMD", "verify_cmd", ""),
        default_pipeline=str(s_yaml.get("default_pipeline", "pipeline-1")),
        thresholds=thresholds,
        pipelines=_yaml(CONFIG_DIR / "pipelines.yaml"),
        roster=_yaml(CONFIG_DIR / "roster.yaml"),
        embed_provider=pick("EMBED_PROVIDER", "embed_provider", "stub"),
        embed_base_url=pick("EMBED_BASE_URL", "embed_base_url", ""),
        embed_model=pick("EMBED_MODEL", "embed_model", ""),
        embed_api_key=os.getenv("EMBED_API_KEY", ""),
    )
