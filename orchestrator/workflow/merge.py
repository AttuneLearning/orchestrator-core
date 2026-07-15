"""Merge logic for workflow profiles across three layers (defaults, repo, workspace).

Pure module — no file I/O, no imports from orchestrator internals.
Merges YAML-parsed dicts into a final Profile with per-step REPLACE semantics
and last-writer-wins for scalars (stack, services).

Hard rules from §2.1:
  - Later layer defining a step REPLACES that step's entire value (no deep list merging).
  - `stack` and `services` scalars: last-writer-wins.
  - `permissions:` key in repo layer is IGNORED and recorded as a warning
    (repo can never self-authorize; authority lives in workspace manifest).
  - Unknown step names raise ProfileError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.workflow.models import (
    STEP_NAMES,
    ON_FAIL,
    Profile,
    RequiredAction,
    WorkflowStep,
)


class ProfileError(Exception):
    """Raised when a profile dict has invalid structure or unknown step names."""

    pass


def parse_profile_dict(raw: dict, source: str) -> dict:
    """Normalize one profile layer (dict from YAML) to canonical form.

    Input structure: step -> list[action-dict] OR step -> {role: list[action-dict]}

    Output structure: step -> {"actions": [...], "by_role": {...}}
    where each action dict is stamped with the source field.

    Validates:
      - All top-level keys are step names (STEP_NAMES) or allowed scalars (stack, services, permissions).
      - All step names are in STEP_NAMES.
      - Each action has exactly one of run or builtin (non-empty after strip).
      - Each action's on_fail is in ON_FAIL.
      - Each action's timeout > 0.

    Args:
        raw: Parsed YAML dict. Scalar keys: stack, services. Step keys: STEP_NAMES.
        source: Provenance label ("default", "repo", "workspace") to stamp on actions.

    Returns:
        Normalized dict with the same top-level scalars and steps in canonical form.

    Raises:
        ProfileError: If any top-level key is unknown, any step name is unknown, or any action has invalid shape.
    """
    normalized = {}

    # Copy scalar keys (stack, services, permissions, service_endpoints) and validate other non-step keys
    for key in raw:
        if key not in STEP_NAMES:
            # Only allow specific scalar keys
            if key not in ("stack", "services", "permissions", "service_endpoints"):
                raise ProfileError(
                    f"unknown top-level key {key!r}; valid steps: {', '.join(STEP_NAMES)}"
                )
            # Copy scalars and permissions directly
            normalized[key] = raw[key]

    # Normalize each step
    for step_name in STEP_NAMES:
        if step_name not in raw:
            continue

        step_value = raw[step_name]

        # Special case: "services" can be either a scalar (list of strings) or a step
        if step_name == "services" and isinstance(step_value, list):
            # Check if it's a list of strings (scalar) or action dicts (step)
            if not step_value or isinstance(step_value[0], str):
                # Scalar: list of service names like ["mongo", "redis"] (or empty list)
                normalized["services"] = step_value
                continue

        # Parse step value: either a list or a dict with roles
        if isinstance(step_value, list):
            # Simple list of actions (role-agnostic)
            actions = _parse_action_list(step_value, source, step_name)
            normalized[step_name] = {"actions": actions, "by_role": {}}

        elif isinstance(step_value, dict):
            # Check if it's a role map (all values are lists) or a plain action dict
            # A role map has string keys and list values
            if all(isinstance(v, list) for v in step_value.values()):
                # Role map: {role: list[action-dict]}
                by_role = {}
                for role, actions_list in step_value.items():
                    by_role[role] = _parse_action_list(
                        actions_list, source, f"{step_name}[{role}]"
                    )
                normalized[step_name] = {"actions": (), "by_role": by_role}
            else:
                # Single action dict (has run/builtin/etc keys)
                # Treat as a list with one action
                actions = _parse_action_list([step_value], source, step_name)
                normalized[step_name] = {"actions": actions, "by_role": {}}

        else:
            raise ProfileError(
                f"step {step_name} must be a list or dict, got {type(step_value).__name__}"
            )

    return normalized


def _parse_action_list(
    actions_list: list, source: str, context: str
) -> tuple:
    """Parse a list of action dicts and stamp with source.

    Args:
        actions_list: List of action dicts (from YAML).
        source: Provenance label to add to each action.
        context: Context string for error messages (step name or step[role]).

    Returns:
        Tuple of action dicts, each with source field added/updated.

    Raises:
        ProfileError: If any action has invalid shape.
    """
    result = []

    for i, action_dict in enumerate(actions_list):
        if not isinstance(action_dict, dict):
            raise ProfileError(
                f"action {i} in {context} must be a dict, got {type(action_dict).__name__}"
            )

        # Copy and add source
        parsed_action = dict(action_dict)
        parsed_action["source"] = source

        # Type-check run, builtin, sentinel before using them
        run_val = parsed_action.get("run")
        if run_val is not None and not isinstance(run_val, str):
            raise ProfileError(
                f"action {i} in {context}: 'run' must be str, got {type(run_val).__name__}"
            )

        builtin_val = parsed_action.get("builtin")
        if builtin_val is not None and not isinstance(builtin_val, str):
            raise ProfileError(
                f"action {i} in {context}: 'builtin' must be str, got {type(builtin_val).__name__}"
            )

        sentinel_val = parsed_action.get("sentinel")
        if sentinel_val is not None and not isinstance(sentinel_val, str):
            raise ProfileError(
                f"action {i} in {context}: 'sentinel' must be str, got {type(sentinel_val).__name__}"
            )

        # Type-check when_changed is a list of str
        when_changed_val = parsed_action.get("when_changed")
        if when_changed_val is not None:
            if not isinstance(when_changed_val, list):
                raise ProfileError(
                    f"action {i} in {context}: 'when_changed' must be list, got {type(when_changed_val).__name__}"
                )
            for j, item in enumerate(when_changed_val):
                if not isinstance(item, str):
                    raise ProfileError(
                        f"action {i} in {context}: 'when_changed[{j}]' must be str, got {type(item).__name__}"
                    )

        # Type-check timeout is int (bool excluded, since bool is subclass of int)
        timeout_val = parsed_action.get("timeout")
        if timeout_val is not None:
            if isinstance(timeout_val, bool) or not isinstance(timeout_val, int):
                raise ProfileError(
                    f"action {i} in {context}: 'timeout' must be int, got {type(timeout_val).__name__}"
                )

        # Now validate the action shape with stripped values
        run = (parsed_action.get("run") or "").strip()
        builtin = (parsed_action.get("builtin") or "").strip()

        # Exactly one of run or builtin must be set
        if not run and not builtin:
            raise ProfileError(
                f"action {i} in {context} has neither run nor builtin set"
            )
        if run and builtin:
            raise ProfileError(
                f"action {i} in {context} has both run and builtin set"
            )

        # Validate on_fail
        on_fail = parsed_action.get("on_fail", "block")
        if on_fail not in ON_FAIL:
            raise ProfileError(
                f"action {i} in {context} has invalid on_fail: {on_fail} "
                f"(must be one of {ON_FAIL})"
            )

        # Validate timeout
        timeout = parsed_action.get("timeout", 300)
        if timeout <= 0:
            raise ProfileError(
                f"action {i} in {context} has timeout <= 0: {timeout}"
            )

        result.append(parsed_action)

    return tuple(result)


def merge_layers(
    defaults: dict | None,
    repo: dict | None,
    workspace: dict | None,
) -> Profile:
    """Merge three layers of profile dicts into a final Profile.

    Precedence order (lowest to highest):
      1. defaults (engine defaults)
      2. repo (from <repo>/.orchestrator/workflow.yaml)
      3. workspace (from operator's workspace manifest)

    Semantics:
      - Per-step REPLACE: if a layer defines a step, it replaces that step entirely
        (no deep list merging). A role-scoped step in an overlay replaces a
        role-agnostic step in the base, and vice versa.
      - Scalars (stack, services): last-writer-wins.
      - Repo layer `permissions:` key is IGNORED and recorded as a warning
        (repo files must never self-authorize).
      - Unknown step names raise ProfileError (this should not happen if layers
        are pre-parsed, but we validate for safety).

    Args:
        defaults: Normalized default profile dict (from engine defaults/adapter).
        repo: Normalized repo profile dict, or None.
        workspace: Normalized workspace manifest dict, or None.

    Returns:
        Profile object with merged steps and final stack/services.

    Raises:
        ProfileError: If any layer has unknown step names.
    """
    warnings = []

    # Check for permissions in repo layer (red flag for security)
    if repo and "permissions" in repo:
        warnings.append(
            "repo-layer permissions: ignored (repo files must never self-authorize; "
            "authority is workspace-manifest only)"
        )
        # Don't include permissions in the merge

    # Start with defaults (or empty if no defaults)
    merged = dict(defaults or {})

    # Merge repo layer (if present)
    if repo:
        merged = _merge_layer(merged, repo, "repo")

    # Merge workspace layer (if present)
    if workspace:
        merged = _merge_layer(merged, workspace, "workspace")

    # Convert merged dict to Profile
    profile_steps = {}
    for step_name in STEP_NAMES:
        if step_name in merged:
            step_dict = merged[step_name]

            # Skip if this is a scalar (e.g., services: ["mongo", "redis"])
            if not isinstance(step_dict, dict) or "actions" not in step_dict:
                continue

            actions = tuple(step_dict.get("actions", []))
            by_role = step_dict.get("by_role", {})

            # Convert action dicts to RequiredAction objects
            action_objs = tuple(
                RequiredAction(
                    run=a.get("run", ""),
                    builtin=a.get("builtin", ""),
                    when_changed=tuple(a.get("when_changed", [])),
                    sentinel=a.get("sentinel", ""),
                    on_fail=a.get("on_fail", "block"),
                    timeout=a.get("timeout", 300),
                    source=a.get("source", "default"),
                    args=a.get("args", ""),
                )
                for a in actions
            )

            by_role_objs = {}
            for role, role_actions in by_role.items():
                by_role_objs[role] = tuple(
                    RequiredAction(
                        run=a.get("run", ""),
                        builtin=a.get("builtin", ""),
                        when_changed=tuple(a.get("when_changed", [])),
                        sentinel=a.get("sentinel", ""),
                        on_fail=a.get("on_fail", "block"),
                        timeout=a.get("timeout", 300),
                        source=a.get("source", "default"),
                        args=a.get("args", ""),
                    )
                    for a in role_actions
                )

            profile_steps[step_name] = WorkflowStep(
                name=step_name,
                actions=action_objs,
                by_role=by_role_objs,
            )

    # Extract scalars with last-writer-wins
    stack = merged.get("stack", "")
    services = tuple(merged.get("services", []))

    return Profile(
        stack=stack,
        services=services,
        steps=profile_steps,
        warnings=tuple(warnings),
    )


def _merge_layer(base: dict, overlay: dict, layer_name: str) -> dict:
    """Merge one layer onto the base dict using REPLACE semantics for steps.

    Assumes overlay has been pre-parsed and validated by parse_profile_dict.
    Validation of unknown keys happens at parse time, not merge time.

    Args:
        base: The base merged dict so far.
        overlay: The layer to merge in (must be pre-parsed).
        layer_name: Name of the layer (for context in comments).

    Returns:
        Merged dict.
    """
    result = dict(base)

    # Merge steps: overlay step REPLACES base step entirely
    for step_name in STEP_NAMES:
        if step_name in overlay:
            result[step_name] = overlay[step_name]

    # Merge scalars: last-writer-wins
    if "stack" in overlay:
        result["stack"] = overlay["stack"]
    if "services" in overlay:
        result["services"] = overlay["services"]
    if "service_endpoints" in overlay:
        result["service_endpoints"] = overlay["service_endpoints"]

    return result
