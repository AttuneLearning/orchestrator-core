import importlib.util
import os
import subprocess
from pathlib import Path

from orchestrator import config
from orchestrator.config import load_settings


def test_model_profile_defaults_are_available(monkeypatch):
    monkeypatch.setattr(config, "_yaml", lambda _path: {})
    monkeypatch.delenv("ORCH_INSTANCE", raising=False)
    monkeypatch.delenv("ROSTER_FILE", raising=False)

    settings = load_settings()

    assert settings.model_profiles["digitalocean"]["base_url"] == "https://inference.do-ai.run/v1"
    assert settings.model_profiles["digitalocean"]["model"] == "deepseek-v4-pro"
    assert settings.model_profiles["digitalocean"]["wire_api"] == "chat"
    assert settings.model_profiles["digitalocean"]["api_key_env"] == "MODEL_ACCESS_KEY"
    assert settings.orch_manager_codex["profile"] == "digitalocean"


def test_launcher_model_settings_preserves_chat_wire_api():
    path = Path("templates/project-launchers/agent-launchers/model-settings.py")
    spec = importlib.util.spec_from_file_location("model_settings", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._wire_api("chat") == "chat"


def test_codex_digitalocean_launcher_uses_chat_wire_api(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    script = Path("templates/project-launchers/agent-launchers/runtimes/codex.sh").resolve()

    env = os.environ.copy()
    env.update({
        "ORCH_LAUNCH_DRY_RUN": "1",
        "LAUNCHER_DIR": str(script.parent.parent),
        "PROMPT_FILE": str(prompt),
        "ROLE": "orch-manager",
        "PROJECT": "test-project",
        "ORCH": str(Path.cwd()),
        "WORKTREE": str(tmp_path),
    })
    proc = subprocess.run(
        [str(script), "--digital-ocean-config"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "model_providers.digitalocean.wire_api=" in proc.stdout
    assert "chat" in proc.stdout
    assert "wire_api=\\\"responses\\\"" not in proc.stdout


def test_engine_reasoner_profile_maps_to_openai_fields(monkeypatch):
    def fake_yaml(path: Path):
        if path.name == "settings.yaml":
            return {
                "model_profiles": {
                    "qwen-local": {
                        "base_url": "http://localhost:8081/v1",
                        "model": "qwen3",
                        "api_key": "local-secret",
                        "api_key_env": "QWEN_REASONER_KEY",
                    }
                },
                "engine_reasoner": {
                    "profile": "qwen-local",
                    "model": "qwen3-reasoner",
                },
            }
        return {}

    monkeypatch.setattr(config, "_yaml", fake_yaml)
    for name in ("ORCH_INSTANCE", "REASONER", "REASONER_BASE_URL", "REASONER_MODEL", "REASONER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWEN_REASONER_KEY", "env-secret")

    settings = load_settings()

    assert settings.reasoner == "openai"
    assert settings.reasoner_base_url == "http://localhost:8081/v1"
    assert settings.reasoner_model == "qwen3-reasoner"
    assert settings.reasoner_api_key == "env-secret"


def test_engine_reasoner_profile_keeps_env_overrides(monkeypatch):
    def fake_yaml(path: Path):
        if path.name == "settings.yaml":
            return {
                "model_profiles": {
                    "qwen-local": {
                        "base_url": "http://localhost:8081/v1",
                        "model": "qwen3",
                        "api_key": "local-secret",
                    }
                },
                "engine_reasoner": {"profile": "qwen-local"},
            }
        return {}

    monkeypatch.setattr(config, "_yaml", fake_yaml)
    monkeypatch.delenv("ORCH_INSTANCE", raising=False)
    monkeypatch.setenv("REASONER_BASE_URL", "http://override/v1")
    monkeypatch.setenv("REASONER_MODEL", "override-model")

    settings = load_settings()

    assert settings.reasoner == "openai"
    assert settings.reasoner_base_url == "http://override/v1"
    assert settings.reasoner_model == "override-model"


def test_instance_codex_profile_override_is_loaded(monkeypatch):
    def fake_yaml(path: Path):
        if path.name == "settings.yaml":
            return {
                "model_profiles": {
                    "digitalocean": {
                        "base_url": "https://inference.do-ai.run/v1",
                        "model": "deepseek-v4-pro",
                        "api_key_env": "MODEL_ACCESS_KEY",
                        "wire_api": "chat",
                    }
                },
                "orch_manager_codex": {"profile": "digitalocean"},
            }
        if path.name == "instances.yaml":
            return {
                "instances": {
                    "tendcharting": {
                        "label": "TendCharting (EHR)",
                        "database_url": "postgresql://orchestrator@localhost:5432/tendcharting",
                        "roster_file": "config/roster.tendcharting.yaml",
                        "settings": {
                            "orch_manager_codex": {
                                "profile": "digitalocean",
                                "model": "deepseek-v4-pro",
                                "reasoning_effort": "high",
                            }
                        },
                    }
                }
            }
        return {}

    monkeypatch.setattr(config, "_yaml", fake_yaml)
    monkeypatch.delenv("ORCH_INSTANCE", raising=False)
    settings = load_settings(instance="tendcharting")

    assert settings.orch_manager_codex["profile"] == "digitalocean"
    assert settings.orch_manager_codex["model"] == "deepseek-v4-pro"
    assert settings.orch_manager_codex["reasoning_effort"] == "high"
