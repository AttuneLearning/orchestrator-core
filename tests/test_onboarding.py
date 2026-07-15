"""Onboarding: tier recommendation, drop-in instance config + instances.d merge,
and the `doctor` preflight / `init` dry-run CLI paths.
"""

from __future__ import annotations

from argparse import Namespace

import yaml

from orchestrator import cli, config, onboarding
from orchestrator import decomposition as dec


# --------------------------------------------------------------------------- #
# Pure: tier recommendation from fleet models
# --------------------------------------------------------------------------- #

def test_recommend_tier_weakest_link_wins():
    assert onboarding.recommend_tier(["qwen-local"]) == dec.REMEDIAL
    # a strong model alongside a weak one still recommends remedial (weakest link)
    assert onboarding.recommend_tier(["claude-opus-4-8", "qwen-local"]) == dec.REMEDIAL
    assert onboarding.recommend_tier(["mistral-7b"]) == dec.REMEDIAL


def test_recommend_tier_high_needs_a_top_model():
    assert onboarding.recommend_tier(["claude-opus-4-8"]) == dec.HIGH
    assert onboarding.recommend_tier(["deepseek-v4-pro", "claude-sonnet-5"]) == dec.HIGH
    # strong but no top-tier model -> mid
    assert onboarding.recommend_tier(["claude-haiku-4-5", "claude-sonnet-5"]) == dec.MID


def test_recommend_tier_qwen_max_is_not_remedial():
    # the strong 'qwen-max' must not be dragged to remedial by the 'qwen' stem
    assert onboarding.recommend_tier(["qwen-max"]) == dec.MID


def test_recommend_tier_empty_is_default():
    assert onboarding.recommend_tier([]) == dec.DEFAULT_TIER
    assert onboarding.recommend_tier(["", "  "]) == dec.DEFAULT_TIER


# --------------------------------------------------------------------------- #
# Pure: drop-in instance config
# --------------------------------------------------------------------------- #

def test_build_and_render_dropin_roundtrips():
    entry = onboarding.build_instance_entry(
        label="Demo", database_url="postgresql://x@localhost/demo",
        roster_file="config/roster.yaml", tier="remedial")
    body = onboarding.render_instances_dropin("demo", entry)
    doc = yaml.safe_load(body)
    got = doc["instances"]["demo"]
    assert got["database_url"] == "postgresql://x@localhost/demo"
    assert got["settings"]["decomposition_tier"] == "remedial"
    assert got["roster_file"] == "config/roster.yaml"


# --------------------------------------------------------------------------- #
# Pure: doctor summary exit codes
# --------------------------------------------------------------------------- #

def test_summarize_exit_codes():
    C = onboarding.Check
    code, rep = onboarding.summarize([C("a", onboarding.PASS)])
    assert code == 0 and "READY" in rep and "with warnings" not in rep
    code, rep = onboarding.summarize([C("a", onboarding.PASS), C("b", onboarding.WARN)])
    assert code == 0 and "with warnings" in rep
    code, rep = onboarding.summarize([C("a", onboarding.FAIL), C("b", onboarding.WARN)])
    assert code == 1 and "NOT READY" in rep


# --------------------------------------------------------------------------- #
# Config: instances.d drop-ins merge over instances.yaml
# --------------------------------------------------------------------------- #

def test_instances_d_dropins_merge(monkeypatch, tmp_path):
    (tmp_path / "instances.yaml").write_text("instances:\n  base:\n    database_url: url-base\n")
    d = tmp_path / "instances.d"
    d.mkdir()
    # full {instances: {...}} form
    (d / "proj.yaml").write_text("instances:\n  proj:\n    database_url: url-proj\n")
    # bare {<name>: {...}} form
    (d / "bare.yaml").write_text("bare:\n  database_url: url-bare\n")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    inst = config._load_instances()
    assert set(inst) == {"base", "proj", "bare"}
    assert inst["proj"]["database_url"] == "url-proj"
    assert inst["bare"]["database_url"] == "url-bare"


# --------------------------------------------------------------------------- #
# CLI: doctor against the live test DB, and init --dry-run
# --------------------------------------------------------------------------- #

def test_doctor_ready_on_migrated_db(settings, pool):
    checks = cli._run_doctor_checks(settings)
    by = {c.name: c.status for c in checks}
    assert by["database"] == onboarding.PASS
    assert by["migrations"] == onboarding.PASS
    assert by["pipelines"] == onboarding.PASS
    assert by["roster"] == onboarding.PASS
    # empty agents/goals are warnings, not failures — the run is still "ready".
    code, _ = onboarding.summarize(checks)
    assert code == 0


def test_init_dry_run_plans_without_side_effects(settings, capsys):
    dropin = cli.REPO_ROOT / "config" / "instances.d" / "probe-xyz.yaml"
    assert not dropin.exists()
    args = Namespace(
        project="probe-xyz", label=None,
        database_url="postgresql://x@localhost/probe_xyz",
        roster_file="config/roster.yaml", decomposition_tier=None,
        models="qwen-local", teams="backend", runtime="api",
        yes=True, force=False, dry_run=True)
    rc = cli._cmd_init(args, settings)
    out = capsys.readouterr().out
    assert rc == 0
    assert "decomposition tier : remedial" in out   # recommended from qwen-local
    assert "would write" in out
    assert not dropin.exists()                        # dry-run wrote nothing


# --------------------------------------------------------------------------- #
# Workflow profile lints (WP-17)
# --------------------------------------------------------------------------- #

def test_doctor_workflow_lint4_enabled_without_manifest(settings, pool):
    """Lint 4: workflow_profile=enabled but workspace_manifest unset -> FAIL."""
    # Set workflow_profile to enabled without a manifest
    from copy import deepcopy
    custom_settings = deepcopy(settings)
    custom_settings.workflow_profile = "enabled"
    custom_settings.workspace_manifest = ""

    checks = cli._run_doctor_checks(custom_settings)
    workflow_checks = [c for c in checks if c.name == "workflow_profile"]

    # Should have a FAIL check about missing manifest
    assert any(c.status == onboarding.FAIL and "workspace_manifest" in c.detail
               for c in workflow_checks)


def test_doctor_workflow_lint5_repo_permissions(settings, pool, tmp_path):
    """Lint 5: repo profile with permissions: key -> FAIL."""
    from copy import deepcopy

    # Create a temporary worktree with a repo profile containing permissions
    worktree = tmp_path / "test_repo"
    worktree.mkdir()

    # Initialize as git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)

    # Create repo profile with permissions (self-authorization attempt)
    orch_dir = worktree / ".orchestrator"
    orch_dir.mkdir()
    profile_file = orch_dir / "workflow.yaml"
    profile_file.write_text(
        "prepare:\n"
        "  - run: npm ci\n"
        "permissions:\n"
        "  allow:\n"
        "    - npm ci\n"
    )

    # Create settings with verify_worktrees pointing to this worktree
    custom_settings = deepcopy(settings)
    custom_settings.verify_worktrees = {"test": str(worktree)}

    checks = cli._run_doctor_checks(custom_settings)
    workflow_checks = [c for c in checks if c.name == "workflow_profile"]

    # Should have a FAIL check about repo permissions
    assert any(c.status == onboarding.FAIL and "permissions" in c.detail
               for c in workflow_checks)


def test_doctor_workflow_regression_legacy_no_profile(settings, pool):
    """Regression: legacy flag with no profile -> no new FAILs (stays green)."""
    # Ensure workflow_profile is legacy (default)
    from copy import deepcopy
    custom_settings = deepcopy(settings)
    custom_settings.workflow_profile = "legacy"
    custom_settings.workspace_manifest = ""
    # No verify_worktrees set, so worktree-dependent lints skip

    checks = cli._run_doctor_checks(custom_settings)
    fail_checks = [c for c in checks if c.status == onboarding.FAIL]

    # Should have the same FAILs as before (not new ones from workflow lints)
    # On a clean DB, the only possible FAILs would be from pipelines/roster/etc,
    # not from workflow_profile
    workflow_fails = [c for c in fail_checks if c.name == "workflow_profile"]
    assert len(workflow_fails) == 0, "Legacy flag with no profile should not add workflow_profile FAILs"

    # Overall should still be ready (code == 0)
    code, _ = onboarding.summarize(checks)
    assert code == 0
