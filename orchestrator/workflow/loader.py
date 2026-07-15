"""Workflow profile loader: composes the three profile layers into an effective
Profile, and reads operator-granted Permissions from the workspace manifest.

Layers, lowest to highest precedence (plan §2.1):
  1. Engine defaults (`defaults/workflow.yaml`, shipped inside this package) —
     folded together with the auto-detected stack adapter's `default_steps()`
     when no layer names a `stack` explicitly. A broken/missing engine
     defaults file is a hard error: there is nothing sane to fall back to.
  2. Repo layer (`<worktree>/.orchestrator/workflow.yaml`), read only when the
     file exists.
  3. Workspace layer (the file at `settings.workspace_manifest`), read only
     when the setting is non-empty and the file exists.

Fail-safe (hard rule, plan §3.2): a malformed repo or workspace layer must
NEVER wedge verify. A `ProfileError` (bad shape/unknown keys), a `yaml.YAMLError`
(bad YAML), or an `OSError` (unreadable file) while loading layer 2 or 3 is
caught here, recorded as a human-readable warning on the returned `Profile`,
and that layer is skipped — the remaining layers still compose normally.

`load_permissions()` reads `permissions:` from the workspace manifest ONLY.
The repo layer's `permissions:` key (if it declares one) is dropped and
warned about by `merge.merge_layers` before it ever reaches this module —
authority for `allow`/`deny`/`bypass` lives exclusively with the operator.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from orchestrator.workflow import adapters
from orchestrator.workflow.merge import ProfileError, merge_layers, parse_profile_dict
from orchestrator.workflow.models import Profile, RequiredAction, WorkflowStep
from orchestrator.workflow.permissions import Permissions

# Engine defaults ship inside the installed package. Resolved relative to THIS
# module's file location (never the process CWD) so `load_effective` behaves
# identically no matter where the caller's shell happens to be. loader.py lives
# at <repo-root>/orchestrator/workflow/loader.py, so three `.parent`s reach the
# repo/package root — equivalent to `Path(orchestrator.__file__).parent.parent`.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULTS_WORKFLOW_YAML = _PACKAGE_ROOT / "defaults" / "workflow.yaml"

# Default service endpoints (host:port) for well-known services
_DEFAULT_SERVICE_ENDPOINTS = {
    "mongo": "localhost:27017",
    "redis": "localhost:6379",
    "s3-mock": "localhost:9000",
    "postgres": "localhost:5432",
}


def _expand_services_step(
    profile: Profile, service_endpoints_override: dict[str, str] | None = None
) -> Profile:
    """Expand a Profile's services scalar into a services WorkflowStep.

    If the profile has no services, returns it unchanged. If services are
    present, creates a `services` step with probe-tcp actions for each service.

    Args:
        profile: The Profile with a services tuple (possibly empty).
        service_endpoints_override: Optional dict mapping service names to
            "host:port" overrides (e.g., {"mongo": "10.0.0.5:27017"}).
            Workspace manifest layer wins over defaults.

    Returns:
        The same profile, with a new or replaced `services` WorkflowStep.
    """
    if not profile.services:
        return profile

    endpoints = dict(_DEFAULT_SERVICE_ENDPOINTS)
    if service_endpoints_override:
        endpoints.update(service_endpoints_override)

    # Build probe actions for each service
    actions = []
    for service_name in profile.services:
        endpoint = endpoints.get(service_name, "localhost:0")
        action = RequiredAction(
            builtin="probe-tcp",
            args=f"{service_name}={endpoint}",
            on_fail="escalate",
            timeout=5,
            source="default",
        )
        actions.append(action)

    services_step = WorkflowStep(name="services", actions=tuple(actions))
    steps = dict(profile.steps)
    steps["services"] = services_step

    return Profile(
        stack=profile.stack,
        services=profile.services,
        steps=steps,
        warnings=profile.warnings,
    )


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """Read and `yaml.safe_load` a file into a dict (empty file -> {}).

    Raises OSError (unreadable) or yaml.YAMLError (bad YAML) to the caller;
    this function itself does no fail-safe handling — callers decide that.
    """
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded if isinstance(loaded, dict) else {}


def load_effective(settings: Any, worktree: str | Path, role: str | None = None) -> Profile:
    """Compose the effective Profile for `worktree` from the three layers.

    `role` is accepted for interface symmetry with the rest of the workflow
    API (callers typically go straight on to `profile.step(name).actions_for(role)`);
    the Profile itself carries every role's actions, so no role-based filtering
    happens at load time.

    Args:
        settings: a Settings-like object; only `.workspace_manifest` is read.
        worktree: path to the repo checkout whose repo-layer file (if any) is loaded.
        role: caller's role, accepted but not used to filter at load time.

    Returns:
        The merged Profile, with `warnings` covering both merge.py's own
        warnings (e.g. repo-layer `permissions:` ignored) and any layer this
        loader had to skip due to a fail-safe error.

    Raises:
        RuntimeError: if the engine defaults file is missing or malformed —
        there is no safe fallback for a broken engine install.
    """
    warnings: list[str] = []
    worktree_path = Path(worktree)

    # --- Layer 1a: raw engine defaults (hard error if broken) ---
    try:
        raw_defaults = _load_yaml_dict(DEFAULTS_WORKFLOW_YAML)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"engine workflow defaults are missing or unreadable at "
            f"{DEFAULTS_WORKFLOW_YAML}: {exc}"
        ) from exc

    # --- Layer 2: repo layer (fail-safe: skip + warn) ---
    repo_normalized: dict[str, Any] | None = None
    repo_path = worktree_path / ".orchestrator" / "workflow.yaml"
    if repo_path.is_file():
        try:
            repo_raw = _load_yaml_dict(repo_path)
            repo_normalized = parse_profile_dict(repo_raw, "repo")
            # Warn if repo layer tries to set service_endpoints (repo can't override)
            if repo_raw.get("service_endpoints"):
                warnings.append(
                    "repo-layer service_endpoints: ignored (workspace manifest overrides only)"
                )
                # Remove it from normalized so it doesn't affect the merge
                repo_normalized.pop("service_endpoints", None)
        except Exception as exc:
            warnings.append(
                f"repo workflow profile at {repo_path} ignored ({type(exc).__name__}): {exc}"
            )
            repo_normalized = None

    # --- Layer 3: workspace manifest (fail-safe: skip + warn) ---
    workspace_normalized: dict[str, Any] | None = None
    manifest_str = str(getattr(settings, "workspace_manifest", "") or "")
    if manifest_str:
        manifest_path = Path(manifest_str)
        if manifest_path.is_file():
            try:
                workspace_raw = _load_yaml_dict(manifest_path)
                workspace_normalized = parse_profile_dict(workspace_raw, "workspace")
            except Exception as exc:
                warnings.append(
                    f"workspace manifest at {manifest_path} ignored "
                    f"({type(exc).__name__}): {exc}"
                )
                workspace_normalized = None

    # --- Layer 1b: fold in the auto-detected adapter's default_steps(), but
    # only when NO layer (defaults file, repo, or workspace) named a stack
    # explicitly. Adapter steps are stack-specific and REPLACE the raw
    # defaults file's same-named steps (whole-value replace, same rule as
    # merge.py uses between layers) — the defaults file is a generic
    # baseline; the adapter is authoritative for its own stack. ---
    explicit_stack = (
        bool(raw_defaults.get("stack"))
        or bool(repo_normalized and repo_normalized.get("stack"))
        or bool(workspace_normalized and workspace_normalized.get("stack"))
    )
    combined_defaults_raw = dict(raw_defaults)
    if not explicit_stack:
        detected = adapters.detect_stack(worktree_path)
        if detected:
            adapter = adapters.get_adapter(detected)
            if adapter is not None:
                combined_defaults_raw.update(adapter.default_steps())
            combined_defaults_raw["stack"] = detected

    try:
        defaults_normalized = parse_profile_dict(combined_defaults_raw, "default")
    except ProfileError as exc:
        # The engine's own defaults (plus its own adapter code) failing to
        # parse is a broken install, not a bad user file — hard error.
        raise RuntimeError(f"engine workflow defaults are invalid: {exc}") from exc

    profile = merge_layers(defaults_normalized, repo_normalized, workspace_normalized)
    if warnings:
        profile = dataclasses.replace(profile, warnings=profile.warnings + tuple(warnings))

    # Expand the services scalar (if any) into a services step with probe actions.
    # Service endpoints can be overridden via the workspace manifest's
    # service_endpoints field (which is not a standard step, so merge.py ignores it).
    service_endpoints_override = None
    if workspace_normalized:
        service_endpoints_override = workspace_normalized.get("service_endpoints", {})
    profile = _expand_services_step(profile, service_endpoints_override)

    return profile


def load_permissions(settings: Any) -> Permissions:
    """Load operator-granted Permissions from the workspace manifest ONLY.

    The repo layer never contributes to Permissions (plan §5 — a repo profile
    must never be able to self-authorize); this function does not even look
    at the repo layer. Fail-safe: any missing setting, missing file, or
    malformed YAML/shape yields an empty (all-escalate) Permissions rather
    than raising, so a bad workspace file degrades to "ask for approval",
    never to "wedge" or to "silently trust".

    Args:
        settings: a Settings-like object; only `.workspace_manifest` is read.

    Returns:
        Permissions parsed from the manifest's `permissions:` block, or
        `Permissions()` (empty) when unset/missing/malformed.
    """
    manifest_str = str(getattr(settings, "workspace_manifest", "") or "")
    if not manifest_str:
        return Permissions()

    manifest_path = Path(manifest_str)
    if not manifest_path.is_file():
        return Permissions()

    try:
        raw = _load_yaml_dict(manifest_path)
    except (yaml.YAMLError, OSError):
        return Permissions()

    perms_raw = raw.get("permissions")
    if not isinstance(perms_raw, dict):
        return Permissions()

    allow = perms_raw.get("allow") or []
    deny = perms_raw.get("deny") or []
    if not isinstance(allow, list) or not isinstance(deny, list):
        return Permissions()

    # Filter out non-str entries in allow/deny lists
    allow_strs = tuple(item for item in allow if isinstance(item, str))
    deny_strs = tuple(item for item in deny if isinstance(item, str))

    # bypass must be strictly bool (anything else -> False)
    bypass_val = perms_raw.get("bypass", False)
    bypass = True if isinstance(bypass_val, bool) and bypass_val else False

    return Permissions(
        allow=allow_strs,
        deny=deny_strs,
        bypass=bypass,
    )
