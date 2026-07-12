#!/usr/bin/env python3
"""Resolve dashboard-managed model settings for launcher adapters."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from typing import Any


def _provider_name(profile_name: str) -> str:
    if profile_name in ("digitalocean", "do"):
        return "digitalocean"
    return profile_name.replace("-", "_")


def _shell_line(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"


def _load_settings():
    orch = os.environ.get("ORCH")
    if not orch:
        raise SystemExit("ORCH is required to resolve model settings")
    sys.path.insert(0, orch)
    from orchestrator.config import load_settings  # noqa: PLC0415

    project = os.environ.get("PROJECT") or os.environ.get("ORCH_INSTANCE")
    return load_settings(instance=project) if project else load_settings()


def _section(settings: Any, name: str) -> dict[str, Any]:
    value = getattr(settings, name, {})
    return value if isinstance(value, dict) else {}


def _wire_api(value: Any) -> str:
    return str(value or "responses")


def resolve(section_name: str) -> dict[str, str]:
    settings = _load_settings()
    section = _section(settings, section_name)
    profiles = getattr(settings, "model_profiles", {}) or {}
    profile_name = str(section.get("profile") or "")
    profile = profiles.get(profile_name, {}) if profile_name else {}
    if profile_name and not isinstance(profile, dict):
        profile = {}
    if profile_name and not profile:
        raise SystemExit(f"unknown model profile {profile_name!r} for {section_name}")

    wire_api = _wire_api(profile.get("wire_api"))
    model = str(section.get("model") or profile.get("model") or "")
    api_key_env = str(profile.get("api_key_env") or "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if not api_key:
        api_key = str(profile.get("api_key") or "")
    values = {
        "ORCH_MODEL_PROFILE": profile_name,
        "ORCH_MODEL_PROVIDER": _provider_name(profile_name),
        "ORCH_MODEL_BASE_URL": str(profile.get("base_url") or ""),
        "ORCH_MODEL_NAME": model,
        "ORCH_MODEL_API_KEY": api_key,
        "ORCH_MODEL_API_KEY_ENV": api_key_env,
        "ORCH_MODEL_WIRE_API": wire_api,
        "ORCH_MODEL_REASONING_EFFORT": str(section.get("reasoning_effort") or ""),
        "ORCH_CLAUDE_MODEL": str(_section(settings, "orch_manager_claude").get("model") or ""),
        "ORCH_CLAUDE_ACCOUNT_LABEL": str(_section(settings, "orch_manager_claude").get("account_label") or ""),
    }
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("section", help="settings section, e.g. orch_manager_codex")
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()

    values = resolve(args.section)
    if args.diagnostic:
        print(f"profile={values['ORCH_MODEL_PROFILE'] or '(none)'}")
        print(f"provider={values['ORCH_MODEL_PROVIDER'] or '(none)'}")
        print(f"model={values['ORCH_MODEL_NAME'] or '(none)'}")
        print(f"base_url={values['ORCH_MODEL_BASE_URL'] or '(none)'}")
        print(f"wire_api={values['ORCH_MODEL_WIRE_API'] or '(default)'}")
        print(f"api_key_env={values['ORCH_MODEL_API_KEY_ENV'] or '(none)'}")
        print(f"api_key_configured={'yes' if values['ORCH_MODEL_API_KEY'] else 'no'}")
        if values["ORCH_MODEL_REASONING_EFFORT"]:
            print(f"reasoning_effort={values['ORCH_MODEL_REASONING_EFFORT']}")
        if values["ORCH_CLAUDE_MODEL"]:
            print(f"claude_model={values['ORCH_CLAUDE_MODEL']}")
        if values["ORCH_CLAUDE_ACCOUNT_LABEL"]:
            print(f"claude_account_label={values['ORCH_CLAUDE_ACCOUNT_LABEL']}")
        return 0

    for key, value in values.items():
        print(_shell_line(key, value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
