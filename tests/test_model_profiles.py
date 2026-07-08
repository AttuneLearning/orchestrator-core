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
