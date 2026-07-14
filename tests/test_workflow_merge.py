"""Pure tests for workflow merge logic (no DB, no I/O)."""

import pytest

from orchestrator.workflow.merge import (
    ProfileError,
    merge_layers,
    parse_profile_dict,
)
from orchestrator.workflow.models import Profile, RequiredAction


class TestParseProfileDict:
    """Tests for parse_profile_dict: normalization of a single layer."""

    def test_empty_dict(self):
        """An empty dict parses to an empty dict."""
        result = parse_profile_dict({}, "default")
        assert result == {}

    def test_scalars_copied(self):
        """Scalar keys (stack, services) are copied to the result."""
        raw = {
            "stack": "node",
            "services": ["mongo", "redis"],
        }
        result = parse_profile_dict(raw, "default")
        assert result["stack"] == "node"
        assert result["services"] == ["mongo", "redis"]

    def test_simple_action_list_normalized(self):
        """A step with a list of actions is normalized."""
        raw = {
            "prepare": [
                {"run": "npm ci"},
                {"run": "npm run build"},
            ]
        }
        result = parse_profile_dict(raw, "repo")
        assert "prepare" in result
        assert "actions" in result["prepare"]
        assert "by_role" in result["prepare"]
        assert len(result["prepare"]["actions"]) == 2
        assert result["prepare"]["by_role"] == {}

    def test_action_source_stamped(self):
        """Each action is stamped with the source field."""
        raw = {
            "prepare": [
                {"run": "npm ci"},
            ]
        }
        result = parse_profile_dict(raw, "repo")
        action = result["prepare"]["actions"][0]
        assert action["source"] == "repo"

    def test_role_scoped_actions_normalized(self):
        """A step with role scoping is normalized."""
        raw = {
            "refresh": {
                "dev": [
                    {"run": "git merge main"},
                ],
                "qa": [
                    {"builtin": "rebuild-verify-branch"},
                ],
            }
        }
        result = parse_profile_dict(raw, "default")
        assert "refresh" in result
        assert result["refresh"]["actions"] == ()
        assert "dev" in result["refresh"]["by_role"]
        assert "qa" in result["refresh"]["by_role"]
        assert len(result["refresh"]["by_role"]["dev"]) == 1
        assert len(result["refresh"]["by_role"]["qa"]) == 1

    def test_role_sources_stamped(self):
        """Role-specific actions are stamped with source."""
        raw = {
            "refresh": {
                "qa": [{"builtin": "rebuild-verify-branch"}],
            }
        }
        result = parse_profile_dict(raw, "workspace")
        action = result["refresh"]["by_role"]["qa"][0]
        assert action["source"] == "workspace"

    def test_action_fields_preserved(self):
        """Action fields are preserved during parsing."""
        raw = {
            "prepare": [
                {
                    "run": "npm ci",
                    "when_changed": ["package-lock.json"],
                    "sentinel": "npm-lock",
                    "on_fail": "escalate",
                    "timeout": 600,
                    "args": "",
                }
            ]
        }
        result = parse_profile_dict(raw, "repo")
        action = result["prepare"]["actions"][0]
        assert action["run"] == "npm ci"
        assert action["when_changed"] == ["package-lock.json"]
        assert action["sentinel"] == "npm-lock"
        assert action["on_fail"] == "escalate"
        assert action["timeout"] == 600
        assert action["source"] == "repo"

    def test_neither_run_nor_builtin_raises(self):
        """An action with neither run nor builtin raises ProfileError."""
        raw = {
            "prepare": [{"on_fail": "block"}]
        }
        with pytest.raises(ProfileError, match="neither run nor builtin"):
            parse_profile_dict(raw, "repo")

    def test_both_run_and_builtin_raises(self):
        """An action with both run and builtin raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "builtin": "node-deps"}]
        }
        with pytest.raises(ProfileError, match="both run and builtin"):
            parse_profile_dict(raw, "repo")

    def test_invalid_on_fail_raises(self):
        """An action with invalid on_fail raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "on_fail": "invalid"}]
        }
        with pytest.raises(ProfileError, match="invalid on_fail"):
            parse_profile_dict(raw, "repo")

    def test_zero_timeout_raises(self):
        """An action with timeout=0 raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "timeout": 0}]
        }
        with pytest.raises(ProfileError, match="timeout <= 0"):
            parse_profile_dict(raw, "repo")

    def test_negative_timeout_raises(self):
        """An action with negative timeout raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "timeout": -10}]
        }
        with pytest.raises(ProfileError, match="timeout <= 0"):
            parse_profile_dict(raw, "repo")

    def test_whitespace_stripped_for_run_builtin_check(self):
        """Whitespace in run/builtin is stripped for the both/neither check."""
        # Whitespace-only run should be treated as empty
        raw = {
            "prepare": [{"run": "   ", "builtin": ""}]
        }
        with pytest.raises(ProfileError, match="neither run nor builtin"):
            parse_profile_dict(raw, "repo")

    def test_single_action_dict_as_list(self):
        """A single action dict is treated as a list with one action."""
        raw = {
            "prepare": [
                {"run": "npm ci"}
            ]
        }
        result = parse_profile_dict(raw, "default")
        assert len(result["prepare"]["actions"]) == 1

    def test_all_valid_on_fail_values(self):
        """All valid on_fail values are accepted."""
        for on_fail_val in ["block", "warn", "escalate"]:
            raw = {
                "prepare": [{"run": "npm ci", "on_fail": on_fail_val}]
            }
            result = parse_profile_dict(raw, "default")
            assert result["prepare"]["actions"][0]["on_fail"] == on_fail_val

    def test_multiple_steps_all_normalized(self):
        """Multiple steps are all normalized correctly."""
        raw = {
            "prepare": [{"run": "npm ci"}],
            "verify": [{"run": "npm test"}],
            "cleanup": [{"run": "git clean -fd"}],
        }
        result = parse_profile_dict(raw, "default")
        assert len(result) == 3  # stack/services not present
        assert "prepare" in result
        assert "verify" in result
        assert "cleanup" in result
        for step_name in ["prepare", "verify", "cleanup"]:
            assert "actions" in result[step_name]
            assert "by_role" in result[step_name]


class TestMergeLayers:
    """Tests for merge_layers: combining defaults, repo, and workspace."""

    def test_defaults_only(self):
        """A profile with only defaults merges correctly."""
        raw_defaults = {
            "stack": "node",
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        result = merge_layers(defaults, None, None)
        assert result.stack == "node"
        assert "prepare" in result.steps
        assert len(result.steps["prepare"].actions) == 1

    def test_none_defaults_ok(self):
        """merge_layers handles None defaults gracefully."""
        result = merge_layers(None, None, None)
        assert result.stack == ""
        assert result.services == ()
        assert result.steps == {}
        assert result.warnings == ()

    def test_repo_replaces_default_step(self):
        """A step defined in repo completely replaces the default step (REPLACE semantics)."""
        raw_defaults = {
            "prepare": [
                {"run": "npm ci"},
                {"run": "npm run build"},
            ],
        }
        raw_repo = {
            "prepare": [
                {"run": "npm ci --legacy-peer-deps"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers(defaults, repo, None)
        # Repo step should completely replace default step
        assert len(result.steps["prepare"].actions) == 1
        assert result.steps["prepare"].actions[0].run == "npm ci --legacy-peer-deps"

    def test_workspace_replaces_repo_step(self):
        """Workspace layer completely replaces repo step."""
        raw_repo = {
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        raw_workspace = {
            "prepare": [
                {"run": "custom ci command"},
            ],
        }
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers({}, repo, workspace)
        assert len(result.steps["prepare"].actions) == 1
        assert result.steps["prepare"].actions[0].run == "custom ci command"

    def test_role_scoped_replaces_agnostic(self):
        """A role-scoped step in overlay replaces an agnostic step in base."""
        raw_defaults = {
            "refresh": [
                {"run": "git merge main"},
            ],
        }
        raw_repo = {
            "refresh": {
                "dev": [{"run": "git merge main"}],
                "qa": [{"builtin": "rebuild"}],
            },
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers(defaults, repo, None)
        # Repo's role-scoped step replaces default's agnostic step
        assert len(result.steps["refresh"].actions) == 0
        assert "dev" in result.steps["refresh"].by_role
        assert "qa" in result.steps["refresh"].by_role

    def test_agnostic_replaces_role_scoped(self):
        """A role-agnostic step in overlay replaces a role-scoped step in base."""
        raw_defaults = {
            "refresh": {
                "qa": [{"builtin": "rebuild"}],
            },
        }
        raw_repo = {
            "refresh": [
                {"run": "git merge main"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers(defaults, repo, None)
        # Repo's agnostic step replaces default's role-scoped step
        assert len(result.steps["refresh"].actions) == 1
        assert result.steps["refresh"].by_role == {}

    def test_stack_last_writer_wins(self):
        """Stack field uses last-writer-wins semantics."""
        raw_defaults = {"stack": "node"}
        raw_repo = {"stack": "python"}
        raw_workspace = {"stack": "go"}
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers(defaults, repo, workspace)
        assert result.stack == "go"

    def test_services_last_writer_wins(self):
        """Services field uses last-writer-wins semantics."""
        raw_defaults = {"services": ["mongo"]}
        raw_repo = {"services": ["redis"]}
        raw_workspace = {"services": ["postgres"]}
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers(defaults, repo, workspace)
        assert result.services == ("postgres",)

    def test_repo_permissions_ignored_with_warning(self):
        """permissions: key in repo layer is ignored and recorded as warning."""
        raw_repo = {
            "permissions": {"allow": ["npm ci"], "deny": []},
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers({}, repo, None)
        # permissions should not appear in the result
        assert "permissions" not in result.steps
        # warning should be recorded
        assert any("permissions" in w for w in result.warnings)

    def test_workspace_permissions_allowed(self):
        """permissions: key in workspace layer is not warned (workspace is authoritative)."""
        raw_workspace = {
            "permissions": {"allow": ["npm ci"], "deny": []},
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers({}, None, workspace)
        # This is allowed; workspace can have permissions
        # But this implementation ignores permissions in merge, so it won't be in steps
        assert "permissions" not in result.steps

    def test_steps_not_in_default_added_from_repo(self):
        """Steps present in repo but not defaults are added to the result."""
        raw_repo = {
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers({}, repo, None)
        assert "prepare" in result.steps
        assert len(result.steps["prepare"].actions) == 1

    def test_multiple_steps_merged(self):
        """Multiple steps from different layers are merged correctly."""
        raw_defaults = {
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        raw_repo = {
            "verify": [
                {"run": "npm test"},
            ],
        }
        raw_workspace = {
            "cleanup": [
                {"run": "git clean -fd"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers(defaults, repo, workspace)
        assert "prepare" in result.steps
        assert "verify" in result.steps
        assert "cleanup" in result.steps
        assert len(result.steps) == 3

    def test_step_not_mentioned_in_overlay_preserved(self):
        """A step present in defaults but not in overlay is preserved."""
        raw_defaults = {
            "prepare": [
                {"run": "npm ci"},
            ],
            "verify": [
                {"run": "npm test"},
            ],
        }
        raw_repo = {
            "prepare": [
                {"run": "npm ci --legacy"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        result = merge_layers(defaults, repo, None)
        # verify should be preserved from defaults
        assert "verify" in result.steps
        assert result.steps["verify"].actions[0].run == "npm test"
        # prepare should be replaced by repo
        assert result.steps["prepare"].actions[0].run == "npm ci --legacy"

    def test_action_source_preserved(self):
        """Action source field is preserved through merge."""
        raw_defaults = {
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        result = merge_layers(defaults, None, None)
        assert result.steps["prepare"].actions[0].source == "default"

    def test_three_layer_precedence(self):
        """All three layers are merged in the correct precedence order."""
        raw_defaults = {
            "stack": "default-stack",
            "services": ["default-svc"],
            "prepare": [
                {"run": "default-cmd"},
            ],
        }
        raw_repo = {
            "stack": "repo-stack",
            "services": ["repo-svc"],
            "prepare": [
                {"run": "repo-cmd"},
            ],
        }
        raw_workspace = {
            "stack": "workspace-stack",
            # services not set, should fall back to repo
            "prepare": [
                {"run": "workspace-cmd"},
            ],
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers(defaults, repo, workspace)
        # All three layers should affect the result
        assert result.stack == "workspace-stack"  # workspace wins
        assert result.services == ("repo-svc",)  # repo wins (workspace didn't set)
        assert result.steps["prepare"].actions[0].run == "workspace-cmd"  # workspace wins

    def test_result_is_profile_object(self):
        """The result is a Profile object, not a dict."""
        defaults = {}
        result = merge_layers(defaults, None, None)
        assert isinstance(result, Profile)
        assert hasattr(result, "stack")
        assert hasattr(result, "services")
        assert hasattr(result, "steps")
        assert hasattr(result, "warnings")

    def test_frozen_profile(self):
        """The returned Profile is frozen (immutable)."""
        defaults = {}
        profile = merge_layers(defaults, None, None)
        with pytest.raises(AttributeError):
            profile.stack = "modified"

    def test_repo_permissions_with_workspace_merge(self):
        """Repo permissions are still warned even when workspace layer present."""
        raw_repo = {
            "permissions": {"allow": [], "deny": []},
            "prepare": [
                {"run": "npm ci"},
            ],
        }
        raw_workspace = {
            "prepare": [
                {"run": "npm test"},
            ],
        }
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers({}, repo, workspace)
        assert any("permissions" in w for w in result.warnings)

    def test_complex_merge_with_role_scoping(self):
        """Complex merge with role-scoped actions in multiple layers."""
        raw_defaults = {
            "refresh": [
                {"run": "default-refresh"},
            ],
        }
        raw_repo = {
            "refresh": {
                "qa": [{"run": "repo-qa-refresh"}],
            },
            "verify": [
                {"run": "repo-verify"},
            ],
        }
        raw_workspace = {
            "verify": {
                "dev": [{"run": "workspace-dev-verify"}],
                "qa": [{"run": "workspace-qa-verify"}],
            },
        }
        defaults = parse_profile_dict(raw_defaults, "default")
        repo = parse_profile_dict(raw_repo, "repo")
        workspace = parse_profile_dict(raw_workspace, "workspace")
        result = merge_layers(defaults, repo, workspace)

        # refresh: repo's role-scoped replaces default's agnostic
        assert len(result.steps["refresh"].actions) == 0
        assert "qa" in result.steps["refresh"].by_role

        # verify: workspace's role-scoped replaces repo's agnostic
        assert len(result.steps["verify"].actions) == 0
        assert "dev" in result.steps["verify"].by_role
        assert "qa" in result.steps["verify"].by_role

    def test_actions_converted_to_required_action_objects(self):
        """Action dicts are converted to RequiredAction objects."""
        defaults = {
            "prepare": {
                "actions": [
                    {
                        "run": "npm ci",
                        "when_changed": ["package-lock.json"],
                        "sentinel": "npm-lock",
                        "on_fail": "escalate",
                        "timeout": 600,
                        "source": "default",
                        "args": "",
                    }
                ],
                "by_role": {},
            },
        }
        result = merge_layers(defaults, None, None)
        action = result.steps["prepare"].actions[0]
        assert isinstance(action, RequiredAction)
        assert action.run == "npm ci"
        assert action.when_changed == ("package-lock.json",)
        assert action.sentinel == "npm-lock"
        assert action.on_fail == "escalate"
        assert action.timeout == 600
        assert action.source == "default"
        assert action.args == ""

    def test_by_role_actions_converted_to_required_action(self):
        """Role-specific actions are also converted to RequiredAction objects."""
        defaults = {
            "refresh": {
                "actions": (),
                "by_role": {
                    "qa": [
                        {
                            "builtin": "rebuild-verify-branch",
                            "source": "default",
                            "on_fail": "block",
                        }
                    ],
                },
            },
        }
        result = merge_layers(defaults, None, None)
        action = result.steps["refresh"].by_role["qa"][0]
        assert isinstance(action, RequiredAction)
        assert action.builtin == "rebuild-verify-branch"
        assert action.source == "default"

    def test_empty_services_list(self):
        """Empty services list is preserved."""
        raw_defaults = {"services": []}
        defaults = parse_profile_dict(raw_defaults, "default")
        result = merge_layers(defaults, None, None)
        assert result.services == ()

    def test_unknown_top_level_key_raises_error(self):
        """Unknown top-level keys raise ProfileError."""
        raw_defaults = {
            "unknown-step": [
                {"run": "cmd"},
            ],
        }
        with pytest.raises(ProfileError, match="unknown top-level key"):
            parse_profile_dict(raw_defaults, "default")

    def test_allowed_scalar_keys_do_not_raise(self):
        """Allowed scalar keys (stack, services, permissions) do not raise ProfileError."""
        # Test stack
        raw_stack = {
            "stack": "node",
            "prepare": [{"run": "npm ci"}],
        }
        result = parse_profile_dict(raw_stack, "default")
        assert result["stack"] == "node"

        # Test services
        raw_services = {
            "services": ["mongo", "redis"],
            "prepare": [{"run": "npm ci"}],
        }
        result = parse_profile_dict(raw_services, "default")
        assert result["services"] == ["mongo", "redis"]

        # Test permissions
        raw_permissions = {
            "permissions": {"allow": ["npm ci"], "deny": []},
            "prepare": [{"run": "npm ci"}],
        }
        result = parse_profile_dict(raw_permissions, "default")
        assert result["permissions"] == {"allow": ["npm ci"], "deny": []}

    def test_run_non_string_raises_error(self):
        """An action with run as a non-string (list) raises ProfileError."""
        raw = {
            "prepare": [{"run": ["curl", "evil"]}]
        }
        with pytest.raises(ProfileError, match="'run' must be str"):
            parse_profile_dict(raw, "repo")

    def test_run_int_raises_error(self):
        """An action with run as an int raises ProfileError."""
        raw = {
            "prepare": [{"run": 123}]
        }
        with pytest.raises(ProfileError, match="'run' must be str"):
            parse_profile_dict(raw, "repo")

    def test_builtin_non_string_raises_error(self):
        """An action with builtin as a non-string raises ProfileError."""
        raw = {
            "prepare": [{"builtin": 123}]
        }
        with pytest.raises(ProfileError, match="'builtin' must be str"):
            parse_profile_dict(raw, "repo")

    def test_sentinel_non_string_raises_error(self):
        """An action with sentinel as a non-string raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "sentinel": 123}]
        }
        with pytest.raises(ProfileError, match="'sentinel' must be str"):
            parse_profile_dict(raw, "repo")

    def test_when_changed_non_list_raises_error(self):
        """An action with when_changed as a non-list raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "when_changed": "package-lock.json"}]
        }
        with pytest.raises(ProfileError, match="'when_changed' must be list"):
            parse_profile_dict(raw, "repo")

    def test_when_changed_list_with_non_string_raises_error(self):
        """An action with when_changed containing non-strings raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "when_changed": ["package-lock.json", 123]}]
        }
        with pytest.raises(ProfileError, match="'when_changed\\[1\\]' must be str"):
            parse_profile_dict(raw, "repo")

    def test_timeout_non_int_raises_error(self):
        """An action with timeout as a string raises ProfileError."""
        raw = {
            "prepare": [{"run": "npm ci", "timeout": "abc"}]
        }
        with pytest.raises(ProfileError, match="'timeout' must be int"):
            parse_profile_dict(raw, "repo")

    def test_timeout_bool_raises_error(self):
        """An action with timeout as a bool (True/False) raises ProfileError (bool excluded)."""
        raw = {
            "prepare": [{"run": "npm ci", "timeout": True}]
        }
        with pytest.raises(ProfileError, match="'timeout' must be int"):
            parse_profile_dict(raw, "repo")
