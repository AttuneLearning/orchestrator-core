"""Contract lifecycle validation and normalization. Pure — no I/O, no DB imports.

Implements the state machine, transition rules, and comprehensive batch validation
for contract lifecycle operations (agree, reject, deprecate, supersede, retire).
All functions are hermetic; callers handle DB persistence via repository.py.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# Local copy of the satisfying set (repository._SATISFIED is authoritative;
# keep these in sync by value). Used for destructive-action classification.
SATISFIED = frozenset({"agreed", "live"})

# Directed transition table. Any (frm -> to) not listed is disallowed.
ALLOWED: dict[str, set[str]] = {
    "proposed":   {"agreed", "rejected"},
    "agreed":     {"deprecated", "superseded", "retired"},
    "live":       {"deprecated", "superseded", "retired"},
    "deprecated": {"superseded", "retired"},
    "superseded": {"retired"},
    "retired":    set(),
    "rejected":   set(),
}

# Map action names to their target statuses.
ACTION_TO_STATUS: dict[str, str] = {
    "agree": "agreed",
    "reject": "rejected",
    "deprecate": "deprecated",
    "supersede": "superseded",
    "retire": "retired",
}

# Actions that require a replacement_contract_id to be provided.
REQUIRES_REPLACEMENT = frozenset({"supersede"})

# Statuses that are invalid targets for replacement (self-dead, cannot replace
# with a retired or rejected contract).
_DEAD_REPLACEMENT = frozenset({"retired", "rejected"})


def normalize_path(path: str) -> str:
    """Collapse path parameters so `/x/:id` and `/x/{id}` compare equal.

    Replaces every `:name` segment and every `{name}` token with a canonical
    `{}`, so routes with different param styles hash to the same key.
    """
    # Replace :name with {}
    path = re.sub(r":\w+", "{}", path)
    # Replace {name} with {}
    path = re.sub(r"\{\w+\}", "{}", path)
    return path


def can_transition(frm: str, to: str) -> bool:
    """True iff `to` is a permitted next status from `frm` (ALLOWED table)."""
    return to in ALLOWED.get(frm, set())


def is_destructive(action: str, from_status: str) -> bool:
    """True for actions that pull a contract OUT of the satisfying set.

    Destructive actions: supersede, retire, and deprecate of an agreed/live contract.
    These require explicit confirmation in apply operations.
    """
    if action in ("supersede", "retire"):
        return True
    return action == "deprecate" and from_status in SATISFIED


def validate_batch(
    changes: list[dict[str, Any]],
    contracts_by_id: dict[int, dict[str, Any]],
    project: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Pure validation + normalization of a lifecycle batch.

    Validates every rule from the spec and builds normalized change dicts.
    Destructive operations (supersede, retire, deprecate of active contracts)
    are classified for confirmation gating.

    Returns: (normalized, conflicts, warnings)
      - normalized: list of validated, normalized change dicts (best-effort;
                    non-empty even if conflicts exist, for partial display)
      - conflicts: list of blocking error messages (non-empty => batch invalid)
      - warnings: list of advisory messages (recorded but non-blocking)

    `contracts_by_id` maps contract id -> contract dict with keys:
      id, method, path, status, content_hash, response_dto, type_ref, ...
    `project` is the audited passthrough stamped onto each normalized change.
    """
    conflicts: list[str] = []
    warnings: list[str] = []
    normalized: list[dict[str, Any]] = []

    # Rule 1: changes must be a non-empty list
    if not isinstance(changes, list):
        conflicts.append("changes must be a list")
        return normalized, conflicts, warnings
    if not changes:
        conflicts.append("changes list is empty")
        return normalized, conflicts, warnings

    # Collect all contract IDs (including replacements) for bulk fetch validation.
    seen_ids: set[int] = set()

    # Rule 2: basic structure and schema of each change, plus rule 3 (no dups)
    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            conflicts.append(f"row {i}: change must be a dict")
            continue

        # Validate contract_id is an integer
        contract_id = change.get("contract_id")
        if not isinstance(contract_id, int):
            conflicts.append(f"row {i}: contract_id must be an integer")
            continue

        # Validate action is known
        action = change.get("action")
        if action not in ACTION_TO_STATUS:
            conflicts.append(f"row {i}: unknown action '{action}'")
            continue

        # Rule 3: no duplicate contract_id in batch
        if contract_id in seen_ids:
            conflicts.append(f"duplicate contract_id {contract_id} in batch")
            continue
        seen_ids.add(contract_id)

        # Rule 4: contract must exist and is not an issue ID
        if contract_id not in contracts_by_id:
            conflicts.append(
                f"unknown contract id {contract_id} "
                "(not a contract — is this an issue id?)"
            )
            continue

        contract = contracts_by_id[contract_id]
        from_status = contract.get("status", "")
        to_status = ACTION_TO_STATUS[action]

        # Rule 5: allowed transition
        if not can_transition(from_status, to_status):
            conflicts.append(f"contract {contract_id}: {from_status} -> {to_status} not allowed")
            continue

        # Rule 6: replacement required for supersede
        replacement_contract_id = change.get("replacement_contract_id")
        if action in REQUIRES_REPLACEMENT:
            if replacement_contract_id is None:
                conflicts.append(f"contract {contract_id}: supersede requires replacement_contract_id")
                continue

        # Rule 7: replacement validity (if provided)
        if replacement_contract_id is not None:
            # 7a: replacement must exist
            if replacement_contract_id not in contracts_by_id:
                conflicts.append(f"replacement {replacement_contract_id} unknown")
                continue

            replacement = contracts_by_id[replacement_contract_id]

            # 7b: no self-replacement
            if replacement_contract_id == contract_id:
                conflicts.append(f"contract {contract_id}: cannot replace itself")
                continue

            # 7c: replacement's status must not be in _DEAD_REPLACEMENT
            replacement_status = replacement.get("status", "")
            if replacement_status in _DEAD_REPLACEMENT:
                conflicts.append(f"replacement {replacement_contract_id} is {replacement_status}")
                continue

            # 7d (warning): replacement is not yet agreed/live
            if replacement_status not in SATISFIED:
                warnings.append(
                    f"replacement {replacement_contract_id} is not yet agreed/live"
                )

        # All per-row validation passed; build the normalized change
        normalized.append({
            "contract_id": contract_id,
            "action": action,
            "method": contract.get("method", ""),
            "path": contract.get("path", ""),
            "from_status": from_status,
            "to_status": to_status,
            "replacement_contract_id": replacement_contract_id,
            "destructive": is_destructive(action, from_status),
            "source_ref": change.get("source_ref"),
            "content_hash": contract.get("content_hash"),
            "project": project,
        })

    # Rule 8: single active contract per normalized (method, path)
    # Compute post-batch status for all contracts, then check for duplicates
    post_batch_status: dict[int, str] = {}
    for contract_id, contract in contracts_by_id.items():
        post_batch_status[contract_id] = contract.get("status", "")

    # Apply each normalized change to compute post-batch status
    for norm_change in normalized:
        post_batch_status[norm_change["contract_id"]] = norm_change["to_status"]

    # Check for duplicates: two contracts with same (method, path, normalized)
    # that are both in SATISFIED post-batch
    active_by_route: dict[tuple[str, str], list[int]] = {}
    for contract_id, contract in contracts_by_id.items():
        post_status = post_batch_status[contract_id]
        if post_status in SATISFIED:
            method = contract.get("method", "")
            path = normalize_path(contract.get("path", ""))
            route_key = (method, path)
            if route_key not in active_by_route:
                active_by_route[route_key] = []
            active_by_route[route_key].append(contract_id)

    for (method, path), contract_ids in active_by_route.items():
        if len(contract_ids) > 1:
            ids_str = ", ".join(str(cid) for cid in sorted(contract_ids))
            conflicts.append(
                f"active duplicate for {method} {path} (ids {ids_str})"
            )

    # Rule 9 (warning): notification-family collision
    # Check if both `/notifications*` (top-level) and `/users/me/notifications*`
    # routes are active post-batch
    has_top_level_notif = False
    has_users_me_notif = False
    for contract_id, contract in contracts_by_id.items():
        post_status = post_batch_status[contract_id]
        if post_status in SATISFIED:
            path = contract.get("path", "")
            if path.startswith("/notifications"):
                has_top_level_notif = True
            if path.startswith("/users/me/notifications"):
                has_users_me_notif = True

    if has_top_level_notif and has_users_me_notif:
        warnings.append(
            "notification routes overlap: top-level /notifications* and /users/me/notifications* both active"
        )

    # Rule 10 (warning): canonical-route field completeness
    # Only for action=="agree": contract must have non-empty response_dto and type_ref
    for norm_change in normalized:
        if norm_change["action"] == "agree":
            contract = contracts_by_id[norm_change["contract_id"]]
            response_dto = (contract.get("response_dto") or "").strip()
            type_ref = (contract.get("type_ref") or "").strip()
            if not response_dto or not type_ref:
                warnings.append(
                    f"contract {norm_change['contract_id']} agreed with missing response_dto/type_ref"
                )

    return normalized, conflicts, warnings
