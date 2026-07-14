"""Pure tests for workflow Profile models (no DB, no I/O)."""

import pytest

from orchestrator.workflow.models import (
    ON_FAIL,
    STEP_NAMES,
    Profile,
    RequiredAction,
    WorkflowStep,
    validate,
)


class TestRequiredAction:
    """Tests for the RequiredAction dataclass."""

    def test_action_with_run_command(self):
        """A action with a run command is valid."""
        action = RequiredAction(run="npm test", timeout=300)
        assert action.run == "npm test"
        assert action.builtin == ""
        assert action.timeout == 300

    def test_action_with_builtin_adapter(self):
        """An action with a builtin adapter name is valid."""
        action = RequiredAction(builtin="node-deps-reconcile")
        assert action.builtin == "node-deps-reconcile"
        assert action.run == ""

    def test_action_frozen(self):
        """RequiredAction is frozen (immutable)."""
        action = RequiredAction(run="npm test")
        with pytest.raises(AttributeError):
            action.run = "npm ci"

    def test_action_with_when_changed_globs(self):
        """An action can have when_changed globs."""
        action = RequiredAction(
            run="npm ci",
            when_changed=("package-lock.json", "packages/*/package.json"),
            sentinel="lock-hash",
        )
        assert action.when_changed == ("package-lock.json", "packages/*/package.json")
        assert action.sentinel == "lock-hash"

    def test_action_source_provenance(self):
        """An action tracks its source (default, repo, or workspace)."""
        action = RequiredAction(run="git clean -fd", source="repo")
        assert action.source == "repo"

    def test_action_on_fail_options(self):
        """An action can have on_fail set to block, warn, or escalate."""
        for fail_mode in ON_FAIL:
            action = RequiredAction(run="npm test", on_fail=fail_mode)
            assert action.on_fail == fail_mode

    def test_action_with_args(self):
        """An action can have optional args (for builtins like probe-tcp)."""
        action = RequiredAction(
            builtin="probe-tcp", args="mongo=localhost:27017"
        )
        assert action.args == "mongo=localhost:27017"


class TestWorkflowStep:
    """Tests for the WorkflowStep dataclass and actions_for method."""

    def test_step_basic(self):
        """A step has a name and a tuple of actions."""
        actions = (
            RequiredAction(run="npm ci"),
            RequiredAction(run="npm test"),
        )
        step = WorkflowStep(name="verify", actions=actions)
        assert step.name == "verify"
        assert step.actions == actions

    def test_step_frozen(self):
        """WorkflowStep is frozen."""
        step = WorkflowStep(name="verify")
        with pytest.raises(AttributeError):
            step.name = "cleanup"

    def test_actions_for_returns_role_match_when_present(self):
        """actions_for returns the role-specific list when it matches."""
        role_agnostic = (RequiredAction(run="npm test"),)
        qa_actions = (RequiredAction(run="npm run lint"),)
        step = WorkflowStep(
            name="verify",
            actions=role_agnostic,
            by_role={"qa": qa_actions},
        )
        # Role match wins
        assert step.actions_for("qa") == qa_actions
        # Role mismatch falls back
        assert step.actions_for("dev") == role_agnostic

    def test_actions_for_fallback_to_agnostic(self):
        """actions_for falls back to role-agnostic actions when role has no match."""
        agnostic = (RequiredAction(run="npm test"),)
        step = WorkflowStep(name="verify", actions=agnostic, by_role={})
        assert step.actions_for("qa") == agnostic
        assert step.actions_for("dev") == agnostic

    def test_actions_for_returns_empty_when_neither_present(self):
        """actions_for returns () when neither role match nor agnostic actions exist."""
        step = WorkflowStep(name="verify", actions=(), by_role={})
        assert step.actions_for(None) == ()
        assert step.actions_for("qa") == ()

    def test_actions_for_with_none_role(self):
        """actions_for(None) returns role-agnostic actions."""
        agnostic = (RequiredAction(run="npm test"),)
        step = WorkflowStep(name="verify", actions=agnostic)
        assert step.actions_for(None) == agnostic


class TestProfile:
    """Tests for the Profile dataclass."""

    def test_profile_basic(self):
        """A profile has stack, services, and steps."""
        step = WorkflowStep(name="prepare", actions=(RequiredAction(run="npm ci"),))
        profile = Profile(
            stack="node",
            services=("mongo",),
            steps={"prepare": step},
        )
        assert profile.stack == "node"
        assert profile.services == ("mongo",)
        assert profile.steps == {"prepare": step}

    def test_profile_frozen(self):
        """Profile is frozen."""
        profile = Profile(stack="node")
        with pytest.raises(AttributeError):
            profile.stack = "python"

    def test_profile_step_accessor_returns_step_when_present(self):
        """step() returns the WorkflowStep when it exists."""
        prepare_step = WorkflowStep(
            name="prepare", actions=(RequiredAction(run="npm ci"),)
        )
        profile = Profile(steps={"prepare": prepare_step})
        assert profile.step("prepare") is prepare_step

    def test_profile_step_accessor_returns_empty_step_when_absent(self):
        """step() returns an empty WorkflowStep when the step is not found."""
        profile = Profile(steps={})
        missing_step = profile.step("verify")
        assert missing_step.name == "verify"
        assert missing_step.actions == ()
        assert missing_step.by_role == {}

    def test_profile_warnings_field(self):
        """Profile has a warnings field for fail-safe load errors."""
        profile = Profile(
            stack="node",
            warnings=("repo layer had invalid YAML", "repo-level permissions: ignored"),
        )
        assert profile.warnings == (
            "repo layer had invalid YAML",
            "repo-level permissions: ignored",
        )

    def test_profile_warnings_default_empty(self):
        """Warnings defaults to an empty tuple."""
        profile = Profile()
        assert profile.warnings == ()


class TestValidate:
    """Tests for the validate() function."""

    def test_validate_returns_empty_for_valid_profile(self):
        """A valid profile produces no validation errors."""
        profile = Profile(
            stack="node",
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(RequiredAction(run="npm ci", timeout=300),),
                ),
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="npm test", on_fail="block"),),
                ),
            },
        )
        assert validate(profile) == []

    def test_validate_catches_unknown_step_name(self):
        """validate() catches unknown step names."""
        profile = Profile(
            steps={
                "prepare": WorkflowStep(name="prepare"),
                "unknown-step": WorkflowStep(name="unknown-step"),
            }
        )
        problems = validate(profile)
        assert any("unknown step name: unknown-step" in p for p in problems)

    def test_validate_all_known_step_names_pass(self):
        """All STEP_NAMES are valid."""
        steps = {name: WorkflowStep(name=name) for name in STEP_NAMES}
        profile = Profile(steps=steps)
        problems = validate(profile)
        assert not any("unknown step name" in p for p in problems)

    def test_validate_catches_both_run_and_builtin(self):
        """validate() catches actions with both run and builtin set."""
        profile = Profile(
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(
                        RequiredAction(run="npm ci", builtin="node-deps-reconcile"),
                    ),
                ),
            }
        )
        problems = validate(profile)
        assert any("has both run and builtin set" in p for p in problems)

    def test_validate_catches_neither_run_nor_builtin(self):
        """validate() catches actions with neither run nor builtin set."""
        profile = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="", builtin=""),),
                ),
            }
        )
        problems = validate(profile)
        assert any("has neither run nor builtin set" in p for p in problems)

    def test_validate_catches_invalid_on_fail(self):
        """validate() catches invalid on_fail values."""
        profile = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="npm test", on_fail="invalid"),),
                ),
            }
        )
        problems = validate(profile)
        assert any("invalid on_fail" in p for p in problems)

    def test_validate_catches_timeout_zero(self):
        """validate() catches timeout <= 0."""
        profile = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="npm test", timeout=0),),
                ),
            }
        )
        problems = validate(profile)
        assert any("timeout <= 0" in p for p in problems)

    def test_validate_catches_timeout_negative(self):
        """validate() catches negative timeout."""
        profile = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="npm test", timeout=-10),),
                ),
            }
        )
        problems = validate(profile)
        assert any("timeout <= 0" in p for p in problems)

    def test_validate_checks_role_specific_actions(self):
        """validate() also checks actions in by_role."""
        profile = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    by_role={
                        "qa": (
                            RequiredAction(run="npm test", on_fail="invalid-mode"),
                        ),
                    },
                ),
            }
        )
        problems = validate(profile)
        assert any("verify[qa]" in p and "invalid on_fail" in p for p in problems)

    def test_validate_multiple_problems_reported(self):
        """validate() reports multiple problems in one pass."""
        profile = Profile(
            steps={
                "bad-step": WorkflowStep(
                    name="bad-step",
                    actions=(
                        RequiredAction(run="", builtin="", timeout=-1),
                    ),
                ),
            }
        )
        problems = validate(profile)
        # Should report: unknown step name, neither run/builtin, and bad timeout
        assert len(problems) >= 3
        assert any("unknown step name" in p for p in problems)
        assert any("has neither run nor builtin set" in p for p in problems)
        assert any("timeout <= 0" in p for p in problems)

    def test_validate_all_on_fail_values_valid(self):
        """All ON_FAIL values pass validation."""
        for fail_mode in ON_FAIL:
            profile = Profile(
                steps={
                    "verify": WorkflowStep(
                        name="verify",
                        actions=(RequiredAction(run="npm test", on_fail=fail_mode),),
                    ),
                }
            )
            problems = validate(profile)
            assert not any("invalid on_fail" in p for p in problems)

    def test_validate_whitespace_in_run_and_builtin(self):
        """Whitespace in run/builtin is stripped when checking both/neither."""
        # Whitespace is considered as "set", so run=" " and builtin="" means only run is set
        profile = Profile(
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(RequiredAction(run="  ", builtin=""),),
                ),
            }
        )
        problems = validate(profile)
        # This should fail because neither has stripped content
        assert any("has neither run nor builtin set" in p for p in problems)

    def test_validate_timeout_boundary(self):
        """Timeout of 1 is valid; 0 and -1 are not."""
        # Valid case
        profile_valid = Profile(
            steps={
                "verify": WorkflowStep(
                    name="verify",
                    actions=(RequiredAction(run="npm test", timeout=1),),
                ),
            }
        )
        assert not any("timeout" in p for p in validate(profile_valid))

        # Invalid cases
        for timeout in [0, -1, -100]:
            profile_invalid = Profile(
                steps={
                    "verify": WorkflowStep(
                        name="verify",
                        actions=(RequiredAction(run="npm test", timeout=timeout),),
                    ),
                }
            )
            problems = validate(profile_invalid)
            assert any("timeout <= 0" in p for p in problems)

    def test_validate_sentinel_without_when_changed_is_error(self):
        """An action with sentinel but empty when_changed is an error."""
        profile = Profile(
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(
                        RequiredAction(run="npm ci", sentinel="npm-lock", when_changed=()),
                    ),
                ),
            }
        )
        problems = validate(profile)
        assert any("sentinel requires when_changed" in p for p in problems)

    def test_validate_sentinel_with_when_changed_is_ok(self):
        """An action with sentinel AND when_changed is valid."""
        profile = Profile(
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(
                        RequiredAction(
                            run="npm ci",
                            sentinel="npm-lock",
                            when_changed=("package-lock.json",),
                        ),
                    ),
                ),
            }
        )
        problems = validate(profile)
        assert not any("sentinel requires when_changed" in p for p in problems)

    def test_validate_no_sentinel_no_when_changed_is_ok(self):
        """An action without sentinel and without when_changed is valid."""
        profile = Profile(
            steps={
                "prepare": WorkflowStep(
                    name="prepare",
                    actions=(RequiredAction(run="npm ci", sentinel="", when_changed=()),),
                ),
            }
        )
        problems = validate(profile)
        assert not any("sentinel requires when_changed" in p for p in problems)
