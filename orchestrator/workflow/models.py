"""Workflow Profile dataclasses: step definitions, actions, and profile composition.

Pure module — no I/O, stdlib + typing only. Models the three layers (defaults,
repo, workspace) that compose into a Profile via merge.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Valid step names in a profile. The workflow engine sequences these in order.
STEP_NAMES = ("refresh", "services", "prepare", "verify", "cleanup", "promote")

# Valid on-fail verdicts for an action. block halts the step; warn logs and
# continues; escalate asks for approval (WP-12).
ON_FAIL = ("block", "warn", "escalate")


@dataclass(frozen=True)
class RequiredAction:
    """A single shell command or builtin adapter action to run as part of a step.

    Exactly one of `run` or `builtin` must be set. The `when_changed` globs
    trigger reconciliation; if none match, the action is skipped. The sentinel
    file name (under <gitdir>/orch/) stores a digest to detect stale runs.
    """

    run: str = ""
    """Shell command to execute (exactly one of run/builtin must be set)."""

    builtin: str = ""
    """Named adapter action, e.g. "node-deps-reconcile" (exactly one of run/builtin must be set)."""

    when_changed: tuple[str, ...] = ()
    """Globs relative to worktree root. If set, action runs only if matched files changed."""

    sentinel: str = ""
    """Sentinel file name stored under <gitdir>/orch/. When set, a digest check
    gates re-runs (skips if unchanged). Written on success."""

    on_fail: str = "block"
    """Behavior on action failure: block (halt step) | warn (log, continue) | escalate (ask approval)."""

    timeout: int = 300
    """Command timeout in seconds."""

    source: str = "default"
    """Provenance: default (engine adapter) | repo (from repo layer) | workspace (from workspace layer)."""

    args: str = ""
    """Optional arguments (used by builtin actions, e.g. probe-tcp endpoint override)."""


@dataclass(frozen=True)
class WorkflowStep:
    """A named step in the workflow, with role-agnostic and role-specific actions.

    The step is typically one of STEP_NAMES. Actions are tried in order;
    on_fail: block halts; warn continues; escalate asks for approval.
    """

    name: str
    """Step name (e.g. prepare, verify, cleanup)."""

    actions: tuple[RequiredAction, ...] = ()
    """Role-agnostic actions; tried when no role-specific list matches."""

    by_role: dict[str, tuple[RequiredAction, ...]] = field(default_factory=dict)
    """Role -> actions map. A role match wins; otherwise the role-agnostic list."""

    def actions_for(self, role: str | None) -> tuple[RequiredAction, ...]:
        """Return actions for a role, or fall back to the role-agnostic list.

        Args:
            role: The caller's role (e.g. 'qa', 'dev'), or None.

        Returns:
            Actions for the role if by_role[role] is set; otherwise the
            role-agnostic actions tuple; or () if neither is set.
        """
        if role is not None and role in self.by_role:
            return self.by_role[role]
        return self.actions


@dataclass(frozen=True)
class Profile:
    """The effective workflow profile: stack type, services, and step definitions.

    Composed from three layers (defaults, repo, workspace) via merge.py.
    Provides a step() accessor and a warnings field for fail-safe loading.
    """

    stack: str = ""
    """Runtime stack: node | python | go | rust | "" (undetected)."""

    services: tuple[str, ...] = ()
    """External service names (e.g. mongo, redis) with health probes."""

    steps: dict[str, WorkflowStep] = field(default_factory=dict)
    """Step name -> WorkflowStep map. Typical keys are STEP_NAMES."""

    warnings: tuple[str, ...] = ()
    """Load-time warnings (e.g. repo-layer permissions: ignored, bad YAML). Fail-safe continues."""

    def step(self, name: str) -> WorkflowStep:
        """Return the named step, or an empty WorkflowStep if not found.

        Args:
            name: Step name to look up.

        Returns:
            The WorkflowStep if present, or a WorkflowStep with name=name and
            no actions.
        """
        return self.steps.get(name, WorkflowStep(name=name))


def validate(profile: Profile) -> list[str]:
    """Return a list of human-readable problems in the profile.

    Validates:
    - Unknown step names in the steps dict
    - Actions with both run and builtin set (or neither)
    - Invalid on_fail values
    - Timeout <= 0

    Args:
        profile: Profile to validate.

    Returns:
        List of problem strings. Empty list means the profile is valid.
    """
    problems = []

    # Check step names
    for step_name in profile.steps:
        if step_name not in STEP_NAMES:
            problems.append(f"unknown step name: {step_name}")

    # Check each action
    for step in profile.steps.values():
        # Check role-agnostic actions
        for action in step.actions:
            problems.extend(_validate_action(action, step.name))

        # Check role-specific actions
        for role, actions in step.by_role.items():
            for action in actions:
                problems.extend(_validate_action(action, step.name, role))

    return problems


def _validate_action(
    action: RequiredAction, step_name: str, role: str | None = None
) -> list[str]:
    """Helper to validate a single action.

    Args:
        action: Action to validate.
        step_name: Name of the step (for error messages).
        role: Role name, if this is a role-specific action.

    Returns:
        List of problem strings for this action.
    """
    problems = []
    context = f"{step_name}"
    if role:
        context = f"{step_name}[{role}]"

    # Exactly one of run or builtin must be set
    has_run = bool(action.run.strip())
    has_builtin = bool(action.builtin.strip())
    if has_run and has_builtin:
        problems.append(f"action in {context} has both run and builtin set")
    elif not has_run and not has_builtin:
        problems.append(f"action in {context} has neither run nor builtin set")

    # on_fail must be valid
    if action.on_fail not in ON_FAIL:
        problems.append(
            f"action in {context} has invalid on_fail: {action.on_fail} "
            f"(must be one of {ON_FAIL})"
        )

    # timeout must be positive
    if action.timeout <= 0:
        problems.append(
            f"action in {context} has timeout <= 0: {action.timeout}"
        )

    # sentinel requires when_changed globs
    if action.sentinel and not action.when_changed:
        problems.append(
            f"action in {context} has sentinel set but empty when_changed "
            "(sentinel requires when_changed globs)"
        )

    return problems
