"""Tests for the workflow profile loader (orchestrator/workflow/loader.py) and
the two Settings fields (workspace_manifest, workflow_profile) that drive it.

Pure tests only: tmp_path fixture repos + monkeypatch. No DB, no pool fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator import config
from orchestrator.config import Settings, load_settings
from orchestrator.workflow.loader import (
    DEFAULTS_WORKFLOW_YAML,
    load_effective,
    load_permissions,
)
from orchestrator.workflow.permissions import Permissions


def _write_yaml(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


# --------------------------------------------------------------------------- #
# load_effective: layering, fail-safe, auto-detect
# --------------------------------------------------------------------------- #

class TestLoadEffectiveDefaults:
    def test_defaults_resolve_from_any_cwd(self, tmp_path, monkeypatch):
        # An unrelated CWD must not change what the loader finds — the engine
        # defaults file is resolved relative to the package, never the CWD.
        # When a worktree has a package-lock.json, the node adapter's
        # verify command (npm run typecheck && npm test) is folded in.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create a package-lock.json to auto-detect node stack
        (worktree / "package-lock.json").touch()
        settings = Settings()

        profile = load_effective(settings, worktree)

        verify_actions = profile.step("verify").actions
        assert len(verify_actions) == 1
        assert verify_actions[0].run == "npm run typecheck && npm test"
        assert DEFAULTS_WORKFLOW_YAML.is_file()

    def test_broken_engine_defaults_is_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "orchestrator.workflow.loader.DEFAULTS_WORKFLOW_YAML",
            tmp_path / "does-not-exist.yaml",
        )
        settings = Settings()
        with pytest.raises(RuntimeError):
            load_effective(settings, tmp_path)


class TestRepoLayer:
    def test_repo_layer_honored_when_present(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json to enable node adapter
        (worktree / "package-lock.json").touch()
        _write_yaml(worktree / ".orchestrator" / "workflow.yaml", {
            "prepare": [{"run": "echo repo-prepare", "on_fail": "warn"}],
        })
        settings = Settings()

        profile = load_effective(settings, worktree)

        prepare_actions = profile.step("prepare").actions
        assert len(prepare_actions) == 1
        assert prepare_actions[0].run == "echo repo-prepare"
        assert prepare_actions[0].source == "repo"
        # verify wasn't touched by the repo layer -> still from the node adapter default
        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"
        assert profile.warnings == ()

    def test_repo_layer_with_bad_type_fails_safe(self, tmp_path):
        """Type-confused scalars in repo profile are caught; defaults still returned with warning."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json to enable node adapter defaults
        (worktree / "package-lock.json").touch()
        # Bad type for run: list instead of string
        _write_yaml(worktree / ".orchestrator" / "workflow.yaml", {
            "prepare": [{"run": ["curl", "evil"]}],
        })
        settings = Settings()

        profile = load_effective(settings, worktree)

        # Fail-safe: defaults still returned (from node adapter)
        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"
        # Warning recorded
        assert any("repo" in w for w in profile.warnings)
        assert any("run" in w and "must be str" in w for w in profile.warnings)

    def test_malformed_repo_yaml_falls_back_to_defaults_with_warning(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json for node auto-detect
        (worktree / "package-lock.json").touch()
        repo_file = worktree / ".orchestrator" / "workflow.yaml"
        repo_file.parent.mkdir(parents=True)
        # Invalid YAML syntax (unclosed flow sequence) -> yaml.YAMLError
        repo_file.write_text("prepare: [\n  - run: broken\n", encoding="utf-8")
        settings = Settings()

        profile = load_effective(settings, worktree)

        # Fail-safe: defaults still returned untouched with node adapter's verify.
        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"
        assert any("repo" in w and str(repo_file) in w for w in profile.warnings)

    def test_repo_layer_unknown_step_name_falls_back_with_warning(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json for node auto-detect
        (worktree / "package-lock.json").touch()
        _write_yaml(worktree / ".orchestrator" / "workflow.yaml", {
            "not_a_real_step": [{"run": "echo hi"}],
        })
        settings = Settings()

        profile = load_effective(settings, worktree)

        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"
        assert len(profile.warnings) == 1
        assert "repo" in profile.warnings[0]


class TestWorkspaceLayer:
    def test_workspace_manifest_honored_when_set_and_present(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "cleanup": [{"run": "echo workspace-cleanup"}],
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        profile = load_effective(settings, worktree)

        assert profile.step("cleanup").actions[0].run == "echo workspace-cleanup"
        assert profile.step("cleanup").actions[0].source == "workspace"

    def test_malformed_workspace_manifest_falls_back_to_defaults_with_warning(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json for node adapter defaults
        (worktree / "package-lock.json").touch()
        manifest = tmp_path / "manifest.yaml"
        # Valid YAML, invalid profile shape (unknown top-level key) -> ProfileError
        _write_yaml(manifest, {"totally_bogus_key": True})
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        profile = load_effective(settings, worktree)

        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"
        assert any("workspace" in w and str(manifest) in w for w in profile.warnings)

    def test_missing_workspace_manifest_file_is_silently_skipped(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create package-lock.json for node adapter defaults
        (worktree / "package-lock.json").touch()
        settings = Settings()
        settings.workspace_manifest = str(tmp_path / "does-not-exist.yaml")

        profile = load_effective(settings, worktree)

        # No file at that path -> layer simply absent, no warning (not malformed,
        # just "not configured for this worktree").
        assert profile.warnings == ()
        assert profile.step("verify").actions[0].run == "npm run typecheck && npm test"

    def test_workspace_step_overrides_repo_step(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "package-lock.json").touch()
        _write_yaml(worktree / ".orchestrator" / "workflow.yaml", {
            "prepare": [{"run": "echo repo-prepare"}],
            "verify": [{"run": "echo repo-verify"}],
        })
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "prepare": [{"run": "echo workspace-prepare"}],
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        profile = load_effective(settings, worktree)

        # workspace wins on prepare (the step it touches)...
        assert profile.step("prepare").actions[0].run == "echo workspace-prepare"
        assert profile.step("prepare").actions[0].source == "workspace"
        # ...but repo's verify (untouched by workspace) still wins over the
        # engine default.
        assert profile.step("verify").actions[0].run == "echo repo-verify"


class TestWorkflowStepNeutral:
    def test_python_project_no_npm_steps(self, tmp_path):
        """A Python project (poetry.lock) must not have node-specific steps.

        This is the regression test for FINDING 2: node steps must not leak
        to non-node stacks. Before the fix, defaults included prepare/verify
        with npm commands that would poison non-node projects.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create poetry.lock to auto-detect as python
        (worktree / "poetry.lock").touch()
        settings = Settings()

        profile = load_effective(settings, worktree)

        assert profile.stack == "python"
        # Python adapter (WP-21) now provides default_steps with:
        # - prepare: py-deps-reconcile builtin (sentinel owned by builtin)
        # - verify: python -m pytest -q
        # - cleanup: git reset --hard && git clean -fd
        prepare = profile.step("prepare").actions
        assert len(prepare) == 1
        assert prepare[0].builtin == "py-deps-reconcile"
        # Sentinel is owned by the builtin, not at the action level
        assert prepare[0].sentinel == ""
        assert prepare[0].when_changed == ()

        verify = profile.step("verify").actions
        assert len(verify) == 1
        assert verify[0].run == "python -m pytest -q"

        # Verify npm commands do NOT appear anywhere (no node-specific steps)
        for action in prepare + verify:
            assert "npm" not in action.run.lower()

        cleanup = profile.step("cleanup").actions
        assert len(cleanup) == 1
        assert "git reset --hard && git clean -fd" in cleanup[0].run


class TestAutoDetect:
    def test_auto_detect_fires_when_no_layer_sets_stack(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "package-lock.json").touch()
        settings = Settings()

        profile = load_effective(settings, worktree)

        assert profile.stack == "node"

    def test_auto_detect_skipped_when_repo_layer_sets_stack(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # A lockfile is present (would auto-detect as node)...
        (worktree / "package-lock.json").touch()
        # ...but the repo layer explicitly claims a different stack.
        _write_yaml(worktree / ".orchestrator" / "workflow.yaml", {
            "stack": "python",
        })
        settings = Settings()

        profile = load_effective(settings, worktree)

        assert profile.stack == "python"
        # Auto-detect didn't run because stack was explicit. Engine defaults are
        # now stack-neutral (no prepare/verify in defaults). So verify should be empty.
        assert profile.step("verify").actions == ()

    def test_no_lockfile_no_stack_detected(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        settings = Settings()

        profile = load_effective(settings, worktree)

        assert profile.stack == ""


# --------------------------------------------------------------------------- #
# load_permissions: workspace manifest ONLY
# --------------------------------------------------------------------------- #

class TestLoadPermissions:
    def test_empty_when_workspace_manifest_unset(self):
        settings = Settings()
        assert load_permissions(settings) == Permissions()

    def test_empty_when_workspace_manifest_missing_file(self, tmp_path):
        settings = Settings()
        settings.workspace_manifest = str(tmp_path / "nope.yaml")
        assert load_permissions(settings) == Permissions()

    def test_reads_permissions_from_workspace_manifest(self, tmp_path):
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "permissions": {
                "allow": ["npm ci --no-audit --no-fund"],
                "deny": ["rm -rf /"],
                "bypass": True,
            },
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        perms = load_permissions(settings)

        assert perms.allow == ("npm ci --no-audit --no-fund",)
        assert perms.deny == ("rm -rf /",)
        assert perms.bypass is True

    def test_ignores_repo_layer_entirely(self, tmp_path):
        # load_permissions doesn't even take a worktree argument, so a repo
        # profile's permissions: block (if one foolishly declares one) can
        # never reach it — this test documents that guarantee at the API
        # boundary: only the workspace manifest is ever consulted.
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {"permissions": {"allow": ["ok"]}})
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        perms = load_permissions(settings)

        assert perms.allow == ("ok",)

    def test_malformed_workspace_manifest_yields_empty_permissions(self, tmp_path):
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("permissions: [\n  - broken\n", encoding="utf-8")
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        assert load_permissions(settings) == Permissions()

    def test_non_dict_permissions_block_yields_empty(self, tmp_path):
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {"permissions": "not-a-dict"})
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        assert load_permissions(settings) == Permissions()

    def test_permissions_filters_non_str_entries(self, tmp_path):
        """Non-str entries in allow/deny lists are filtered out."""
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "permissions": {
                "allow": [123, "npm ci", None, "npm run build"],
                "deny": [True, "rm -rf /"],
                "bypass": False,
            },
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        perms = load_permissions(settings)

        assert perms.allow == ("npm ci", "npm run build")
        assert perms.deny == ("rm -rf /",)
        assert perms.bypass is False

    def test_permissions_bypass_non_bool_becomes_false(self, tmp_path):
        """bypass with non-bool value becomes False."""
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "permissions": {
                "allow": ["npm ci"],
                "deny": [],
                "bypass": "yes",  # string, not bool
            },
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        perms = load_permissions(settings)

        assert perms.bypass is False

    def test_permissions_bypass_true_stays_true(self, tmp_path):
        """bypass with explicit True stays True."""
        manifest = tmp_path / "manifest.yaml"
        _write_yaml(manifest, {
            "permissions": {
                "allow": ["npm ci"],
                "deny": [],
                "bypass": True,
            },
        })
        settings = Settings()
        settings.workspace_manifest = str(manifest)

        perms = load_permissions(settings)

        assert perms.bypass is True


# --------------------------------------------------------------------------- #
# Settings fields: workspace_manifest / workflow_profile wiring
# --------------------------------------------------------------------------- #

class TestSettingsFields:
    def test_defaults(self, monkeypatch):
        monkeypatch.setattr(config, "_yaml", lambda _path: {})
        monkeypatch.delenv("ORCH_INSTANCE", raising=False)
        monkeypatch.delenv("WORKSPACE_MANIFEST", raising=False)
        monkeypatch.delenv("WORKFLOW_PROFILE", raising=False)

        settings = load_settings()

        assert settings.workspace_manifest == ""
        assert settings.workflow_profile == "legacy"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setattr(config, "_yaml", lambda _path: {})
        monkeypatch.delenv("ORCH_INSTANCE", raising=False)
        monkeypatch.setenv("WORKSPACE_MANIFEST", "/abs/path/manifest.yaml")
        monkeypatch.setenv("WORKFLOW_PROFILE", "enabled")

        settings = load_settings()

        assert settings.workspace_manifest == "/abs/path/manifest.yaml"
        assert settings.workflow_profile == "enabled"

    def test_instance_settings_block_reaches_settings(self, monkeypatch):
        # Mirrors test_instance_codex_profile_override_is_loaded in
        # tests/test_model_profiles.py: an instance's `settings:` block is
        # merged into s_yaml by _resolve_instance, so workspace_manifest /
        # workflow_profile declared there must reach the returned Settings.
        def fake_yaml(path: Path):
            if path.name == "instances.yaml":
                return {
                    "instances": {
                        "myproject": {
                            "label": "My Project",
                            "database_url": "postgresql://orchestrator@localhost:5432/myproject",
                            "settings": {
                                "workspace_manifest": "/abs/path/to/workspace/manifest.yaml",
                                "workflow_profile": "enabled",
                            },
                        }
                    }
                }
            return {}

        monkeypatch.setattr(config, "_yaml", fake_yaml)
        monkeypatch.delenv("ORCH_INSTANCE", raising=False)
        monkeypatch.delenv("WORKSPACE_MANIFEST", raising=False)
        monkeypatch.delenv("WORKFLOW_PROFILE", raising=False)

        settings = load_settings(instance="myproject")

        assert settings.workspace_manifest == "/abs/path/to/workspace/manifest.yaml"
        assert settings.workflow_profile == "enabled"

    def test_env_wins_over_instance_settings_block(self, monkeypatch):
        def fake_yaml(path: Path):
            if path.name == "instances.yaml":
                return {
                    "instances": {
                        "myproject": {
                            "database_url": "postgresql://orchestrator@localhost:5432/myproject",
                            "settings": {"workflow_profile": "enabled"},
                        }
                    }
                }
            return {}

        monkeypatch.setattr(config, "_yaml", fake_yaml)
        monkeypatch.delenv("ORCH_INSTANCE", raising=False)
        monkeypatch.setenv("WORKFLOW_PROFILE", "legacy")

        settings = load_settings(instance="myproject")

        assert settings.workflow_profile == "legacy"
