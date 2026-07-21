"""Pure unit tests for orchestrator.contract_lifecycle (no DB, no I/O).

Tests the state machine, transition rules, batch validation, and rule
classification (destructive actions, conflicts vs warnings).
"""

from orchestrator import contract_lifecycle as lifecycle


# ==== Test fixtures: contracts and changes ====

def _contract(cid, method="GET", path="/test", status="proposed",
              response_dto="DTO", type_ref="TypeRef", content_hash="hash123"):
    """Helper to build a contract dict for validate_batch."""
    return {
        "id": cid,
        "method": method,
        "path": path,
        "status": status,
        "content_hash": content_hash,
        "response_dto": response_dto,
        "type_ref": type_ref,
    }


def _change(contract_id, action, replacement_contract_id=None, source_ref=None):
    """Helper to build a change dict for validate_batch."""
    change = {"contract_id": contract_id, "action": action}
    if replacement_contract_id is not None:
        change["replacement_contract_id"] = replacement_contract_id
    if source_ref is not None:
        change["source_ref"] = source_ref
    return change


# ==== Allowed transition matrix (can_transition) ====

class TestCanTransition:
    """Test the state machine transition rules."""

    def test_proposed_to_agreed(self):
        """proposed -> agreed is allowed."""
        assert lifecycle.can_transition("proposed", "agreed")

    def test_proposed_to_rejected(self):
        """proposed -> rejected is allowed."""
        assert lifecycle.can_transition("proposed", "rejected")

    def test_proposed_to_deprecated_disallowed(self):
        """proposed -> deprecated is disallowed."""
        assert not lifecycle.can_transition("proposed", "deprecated")

    def test_proposed_to_superseded_disallowed(self):
        """proposed -> superseded is disallowed."""
        assert not lifecycle.can_transition("proposed", "superseded")

    def test_agreed_to_deprecated(self):
        """agreed -> deprecated is allowed."""
        assert lifecycle.can_transition("agreed", "deprecated")

    def test_agreed_to_superseded(self):
        """agreed -> superseded is allowed."""
        assert lifecycle.can_transition("agreed", "superseded")

    def test_agreed_to_retired(self):
        """agreed -> retired is allowed."""
        assert lifecycle.can_transition("agreed", "retired")

    def test_agreed_to_agreed_disallowed(self):
        """agreed -> agreed (self-loop) is disallowed."""
        assert not lifecycle.can_transition("agreed", "agreed")

    def test_live_to_deprecated(self):
        """live -> deprecated is allowed."""
        assert lifecycle.can_transition("live", "deprecated")

    def test_live_to_superseded(self):
        """live -> superseded is allowed."""
        assert lifecycle.can_transition("live", "superseded")

    def test_live_to_retired(self):
        """live -> retired is allowed."""
        assert lifecycle.can_transition("live", "retired")

    def test_deprecated_to_superseded(self):
        """deprecated -> superseded is allowed."""
        assert lifecycle.can_transition("deprecated", "superseded")

    def test_deprecated_to_retired(self):
        """deprecated -> retired is allowed."""
        assert lifecycle.can_transition("deprecated", "retired")

    def test_superseded_to_retired(self):
        """superseded -> retired is allowed."""
        assert lifecycle.can_transition("superseded", "retired")

    def test_retired_is_terminal(self):
        """retired -> * is disallowed (terminal state)."""
        assert not lifecycle.can_transition("retired", "agreed")
        assert not lifecycle.can_transition("retired", "deprecated")
        assert not lifecycle.can_transition("retired", "retired")

    def test_rejected_is_terminal(self):
        """rejected -> * is disallowed (terminal state)."""
        assert not lifecycle.can_transition("rejected", "agreed")
        assert not lifecycle.can_transition("rejected", "proposed")
        assert not lifecycle.can_transition("rejected", "rejected")

    def test_unknown_status_disallowed(self):
        """Unknown status cannot transition anywhere."""
        assert not lifecycle.can_transition("unknown", "agreed")
        assert not lifecycle.can_transition("agreed", "unknown")


# ==== Destructive action classification ====

class TestIsDestructive:
    """Test is_destructive (actions that pull a contract out of SATISFIED)."""

    def test_supersede_is_destructive(self):
        """supersede is destructive regardless of from_status."""
        assert lifecycle.is_destructive("supersede", "proposed")
        assert lifecycle.is_destructive("supersede", "agreed")
        assert lifecycle.is_destructive("supersede", "live")

    def test_retire_is_destructive(self):
        """retire is destructive regardless of from_status."""
        assert lifecycle.is_destructive("retire", "proposed")
        assert lifecycle.is_destructive("retire", "agreed")
        assert lifecycle.is_destructive("retire", "live")

    def test_deprecate_destructive_only_for_satisfied(self):
        """deprecate is destructive only if from_status in SATISFIED."""
        # SATISFIED = {"agreed", "live"}
        assert lifecycle.is_destructive("deprecate", "agreed")
        assert lifecycle.is_destructive("deprecate", "live")
        # Not destructive if already outside SATISFIED
        assert not lifecycle.is_destructive("deprecate", "proposed")
        assert not lifecycle.is_destructive("deprecate", "deprecated")
        assert not lifecycle.is_destructive("deprecate", "superseded")

    def test_agree_not_destructive(self):
        """agree is never destructive."""
        assert not lifecycle.is_destructive("agree", "proposed")

    def test_reject_not_destructive(self):
        """reject is never destructive."""
        assert not lifecycle.is_destructive("reject", "proposed")
        assert not lifecycle.is_destructive("reject", "agreed")


# ==== Path normalization ====

class TestNormalizePath:
    """Test normalize_path (collapse :param and {param} styles)."""

    def test_colon_param_normalized(self):
        """:id is replaced with {}."""
        assert lifecycle.normalize_path("/users/:id") == "/users/{}"
        assert lifecycle.normalize_path("/posts/:post_id") == "/posts/{}"

    def test_brace_param_normalized(self):
        """{id} is replaced with {}."""
        assert lifecycle.normalize_path("/users/{id}") == "/users/{}"
        assert lifecycle.normalize_path("/posts/{post_id}") == "/posts/{}"

    def test_multiple_params_all_normalized(self):
        """Multiple params are all collapsed."""
        assert lifecycle.normalize_path("/users/:user_id/posts/:post_id") == "/users/{}/posts/{}"
        assert lifecycle.normalize_path("/a/{x}/b/:y/c/{z}") == "/a/{}/b/{}/c/{}"

    def test_no_params_unchanged(self):
        """Paths with no params pass through."""
        assert lifecycle.normalize_path("/users") == "/users"
        assert lifecycle.normalize_path("/api/v1/posts") == "/api/v1/posts"

    def test_colon_and_brace_mixed(self):
        """Mixed :id and {id} styles normalize together."""
        assert lifecycle.normalize_path("/users/:id/posts/{post_id}") == "/users/{}/posts/{}"


# ==== Basic validation rules ====

class TestValidateBatchBasicStructure:
    """Test rules 1-3: structure, known actions, no duplicates."""

    def test_changes_must_be_list(self):
        """Rule 1: changes must be a list."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            "not a list",
            {1: _contract(1)},
            "test-project"
        )
        assert "changes must be a list" in conflicts
        assert len(normalized) == 0

    def test_changes_list_cannot_be_empty(self):
        """Rule 1: changes list must be non-empty."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [],
            {1: _contract(1)},
            "test-project"
        )
        assert "changes list is empty" in conflicts

    def test_change_must_be_dict(self):
        """Rule 2: each change must be a dict."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            ["not a dict"],
            {1: _contract(1)},
            "test-project"
        )
        assert any("change must be a dict" in c for c in conflicts)

    def test_contract_id_must_be_integer(self):
        """Rule 2: contract_id must be an integer."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change("not-int", "agree")],
            {1: _contract(1)},
            "test-project"
        )
        assert any("contract_id must be an integer" in c for c in conflicts)

    def test_action_must_be_known(self):
        """Rule 2: action must be in ACTION_TO_STATUS."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "unknown_action")],
            {1: _contract(1)},
            "test-project"
        )
        assert any("unknown action 'unknown_action'" in c for c in conflicts)

    def test_no_duplicate_contract_ids(self):
        """Rule 3: duplicate contract_id in batch is a conflict."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree"), _change(1, "reject")],
            {1: _contract(1)},
            "test-project"
        )
        assert any("duplicate contract_id 1" in c for c in conflicts)


# ==== Contract existence and issue-ID detection ====

class TestValidateBatchContractExistence:
    """Test rule 4: contract must exist and is not an issue ID."""

    def test_unknown_contract_id_rejected(self):
        """Unknown contract_id is a conflict."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(999, "agree")],
            {1: _contract(1)},
            "test-project"
        )
        assert any("unknown contract id 999" in c and "not a contract" in c for c in conflicts)

    def test_issue_id_as_contract_id_detected(self):
        """Error message for unknown ID includes hint about issue IDs."""
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(999, "agree")],
            {1: _contract(1)},
            "test-project"
        )
        assert any("is this an issue id?" in c for c in conflicts)


# ==== Transition and replacement rules ====

class TestValidateBatchTransitions:
    """Test rule 5: allowed transitions."""

    def test_disallowed_transition_is_conflict(self):
        """Transition from proposed -> deprecated is disallowed."""
        contract = _contract(1, status="proposed")
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "deprecate")],  # proposed -> deprecated
            {1: contract},
            "test-project"
        )
        assert any("proposed -> deprecated not allowed" in c for c in conflicts)


class TestValidateBatchReplacementRequired:
    """Test rule 6: replacement required for supersede."""

    def test_supersede_requires_replacement(self):
        """supersede without replacement_contract_id is a conflict."""
        contract = _contract(1, status="agreed")
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede")],  # no replacement
            {1: contract},
            "test-project"
        )
        assert any("supersede requires replacement_contract_id" in c for c in conflicts)

    def test_retire_does_not_require_replacement(self):
        """retire without replacement is allowed."""
        contract = _contract(1, status="agreed")
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "retire")],
            {1: contract},
            "test-project"
        )
        assert len([c for c in conflicts if "require" in c]) == 0
        assert len(normalized) == 1
        assert normalized[0]["to_status"] == "retired"


class TestValidateBatchReplacementValidity:
    """Test rule 7: replacement contract validity."""

    def test_replacement_must_exist(self):
        """Replacement contract_id must exist in contracts_by_id."""
        contracts = {1: _contract(1, status="agreed"), 2: _contract(2)}
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=999)],
            contracts,
            "test-project"
        )
        assert any("replacement 999 unknown" in c for c in conflicts)

    def test_no_self_replacement(self):
        """A contract cannot replace itself."""
        contracts = {1: _contract(1, status="agreed")}
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=1)],
            contracts,
            "test-project"
        )
        assert any("cannot replace itself" in c for c in conflicts)

    def test_replacement_cannot_be_retired(self):
        """Replacement contract cannot be in a dead state (retired)."""
        contracts = {
            1: _contract(1, status="agreed"),
            2: _contract(2, status="retired"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=2)],
            contracts,
            "test-project"
        )
        assert any("replacement 2 is retired" in c for c in conflicts)

    def test_replacement_cannot_be_rejected(self):
        """Replacement contract cannot be rejected."""
        contracts = {
            1: _contract(1, status="agreed"),
            2: _contract(2, status="rejected"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=2)],
            contracts,
            "test-project"
        )
        assert any("replacement 2 is rejected" in c for c in conflicts)

    def test_replacement_not_yet_satisfied_is_warning(self):
        """Replacement not in SATISFIED ({agreed, live}) is a warning."""
        contracts = {
            1: _contract(1, status="agreed"),
            2: _contract(2, status="proposed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=2)],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0  # no blocking conflict
        assert any("replacement 2 is not yet agreed/live" in w for w in warnings)
        assert len(normalized) == 1  # change is still normalized (best-effort)


# ==== Route collision detection ====

class TestValidateBatchRouteCollision:
    """Test rule 8: single active contract per normalized (method, path)."""

    def test_no_duplicate_routes_same_status(self):
        """Two contracts with same (method, path) cannot both be active."""
        contracts = {
            1: _contract(1, method="GET", path="/users/{id}", status="proposed"),
            2: _contract(2, method="GET", path="/users/:id", status="agreed"),
        }
        # Agree contract 1, which makes both active on the same route
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        # Both would become active on the same normalized route -> collision
        assert any("active duplicate for GET /users/{}" in c for c in conflicts)

    def test_no_duplicate_routes_after_batch_change(self):
        """Two contracts can both become active due to a batch change."""
        contracts = {
            1: _contract(1, method="GET", path="/users/{id}", status="proposed"),
            2: _contract(2, method="GET", path="/users/:id", status="agreed"),
        }
        # If we agree contract 1, both become active -> conflict
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert any("active duplicate for GET /users/{}" in c for c in conflicts)

    def test_duplicate_routes_ok_if_one_becomes_inactive(self):
        """Duplicate routes are OK if one becomes inactive."""
        contracts = {
            1: _contract(1, method="GET", path="/users/{id}", status="agreed"),
            2: _contract(2, method="GET", path="/users/:id", status="agreed"),
        }
        # If we deprecate contract 2, only contract 1 remains active -> no conflict
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(2, "deprecate")],
            contracts,
            "test-project"
        )
        assert len([c for c in conflicts if "active duplicate" in c]) == 0

    def test_different_methods_no_collision(self):
        """Different methods do not collide (GET vs POST)."""
        contracts = {
            1: _contract(1, method="GET", path="/users", status="proposed"),
            2: _contract(2, method="POST", path="/users", status="proposed"),
        }
        # Both contracts agreed via the batch -> both become active. Same path,
        # different methods -> the collision key differs -> no conflict. This
        # exercises the real Rule 8 post-batch loop (not the empty-list early
        # return), guarding against a collision-key bug that ignores method.
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree"), _change(2, "agree")],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 2
        assert len([c for c in conflicts if "active duplicate" in c]) == 0

    def test_different_paths_no_collision(self):
        """Different paths do not collide."""
        contracts = {
            1: _contract(1, method="GET", path="/users", status="proposed"),
            2: _contract(2, method="GET", path="/posts", status="proposed"),
        }
        # Both contracts agreed via the batch -> both become active. Same
        # method, different paths -> no conflict. Exercises the real Rule 8
        # post-batch loop, guarding against a collision-key bug that ignores
        # path.
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree"), _change(2, "agree")],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 2
        assert len([c for c in conflicts if "active duplicate" in c]) == 0


# ==== Notification collision warning ====

class TestValidateBatchNotificationCollision:
    """Test rule 9: /notifications vs /users/me/notifications warning."""

    def test_notification_collision_warning(self):
        """Active top-level /notifications* and /users/me/notifications* routes warn."""
        contracts = {
            1: _contract(1, method="GET", path="/notifications", status="proposed"),
            2: _contract(2, method="GET", path="/users/me/notifications", status="proposed"),
        }
        # Agree both contracts, making them both active
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree"), _change(2, "agree")],
            contracts,
            "test-project"
        )
        assert any("notification routes overlap" in w for w in warnings)

    def test_notification_collision_only_if_both_active(self):
        """Collision warning only if both families are active."""
        contracts = {
            1: _contract(1, method="GET", path="/notifications", status="proposed"),
            2: _contract(2, method="GET", path="/users/me/notifications", status="deprecated"),
        }
        # A real change (agree contract 1) drives the post-batch loop. Contract
        # 2 is untouched by the batch and keeps its current (deprecated, i.e.
        # not-active) status, so only the top-level family is active -> no
        # warning. Exercises Rule 9 for real, not via the empty-list shortcut.
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 1
        assert len([w for w in warnings if "notification routes overlap" in w]) == 0

    def test_only_top_level_notifications_no_warning(self):
        """Only top-level /notifications* routes do not trigger warning."""
        contracts = {
            1: _contract(1, method="GET", path="/notifications", status="proposed"),
            2: _contract(2, method="GET", path="/notifications/unread", status="agreed"),
        }
        # Agree contract 1 -> both contracts active, both top-level
        # /notifications* family, no /users/me/notifications* present ->
        # no warning. Exercises the real post-batch loop for Rule 9.
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 1
        assert len([w for w in warnings if "notification routes overlap" in w]) == 0

    def test_only_users_me_notifications_no_warning(self):
        """Only /users/me/notifications* routes do not trigger warning."""
        contracts = {
            1: _contract(1, method="GET", path="/users/me/notifications", status="proposed"),
            2: _contract(2, method="GET", path="/users/me/notifications/unread", status="agreed"),
        }
        # Agree contract 1 -> both contracts active, both /users/me/
        # notifications* family, no top-level /notifications* present ->
        # no warning. Exercises the real post-batch loop for Rule 9.
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 1
        assert len([w for w in warnings if "notification routes overlap" in w]) == 0


# ==== Canonical field completeness warning ====

class TestValidateBatchCanonicalFields:
    """Test rule 10: response_dto and type_ref completeness for agree action."""

    def test_agree_missing_response_dto_warning(self):
        """agree action with empty response_dto generates warning."""
        contracts = {
            1: _contract(1, status="proposed", response_dto="", type_ref="TypeRef"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert any("missing response_dto/type_ref" in w for w in warnings)

    def test_agree_missing_type_ref_warning(self):
        """agree action with empty type_ref generates warning."""
        contracts = {
            1: _contract(1, status="proposed", response_dto="DTO", type_ref=""),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert any("missing response_dto/type_ref" in w for w in warnings)

    def test_agree_missing_both_fields_warning(self):
        """agree action with both fields missing generates warning."""
        contracts = {
            1: _contract(1, status="proposed", response_dto="", type_ref=""),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert any("missing response_dto/type_ref" in w for w in warnings)

    def test_agree_with_complete_fields_no_warning(self):
        """agree action with both fields present does not warn."""
        contracts = {
            1: _contract(1, status="proposed", response_dto="DTO", type_ref="TypeRef"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert len([w for w in warnings if "missing response_dto/type_ref" in w]) == 0

    def test_non_agree_actions_ignore_canonical_fields(self):
        """Non-agree actions do not warn about missing canonical fields."""
        contracts = {
            1: _contract(1, status="agreed", response_dto="", type_ref=""),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "deprecate")],  # not agree
            contracts,
            "test-project"
        )
        assert len([w for w in warnings if "missing response_dto/type_ref" in w]) == 0


# ==== Normalized output validation ====

class TestValidateBatchNormalized:
    """Test that normalized output has correct structure and values."""

    def test_normalized_contains_all_required_fields(self):
        """Normalized change has all required fields."""
        contracts = {1: _contract(1, status="proposed")}
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree")],
            contracts,
            "test-project"
        )
        assert len(normalized) == 1
        norm = normalized[0]
        assert norm["contract_id"] == 1
        assert norm["action"] == "agree"
        assert norm["method"] == "GET"
        assert norm["path"] == "/test"
        assert norm["from_status"] == "proposed"
        assert norm["to_status"] == "agreed"
        assert norm["replacement_contract_id"] is None
        assert norm["destructive"] is False
        assert norm["source_ref"] is None
        assert norm["content_hash"] == "hash123"
        assert norm["project"] == "test-project"

    def test_normalized_carries_replacement_contract_id(self):
        """Normalized change carries replacement_contract_id when provided."""
        contracts = {
            1: _contract(1, status="agreed"),
            2: _contract(2, status="agreed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=2)],
            contracts,
            "test-project"
        )
        assert len(normalized) == 1
        assert normalized[0]["replacement_contract_id"] == 2

    def test_normalized_carries_source_ref(self):
        """Normalized change carries source_ref when provided."""
        contracts = {1: _contract(1, status="proposed")}
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "agree", source_ref="api#v2")],
            contracts,
            "test-project"
        )
        assert len(normalized) == 1
        assert normalized[0]["source_ref"] == "api#v2"

    def test_normalized_destructive_flag_set(self):
        """Normalized change has destructive=True for destructive actions."""
        contracts = {1: _contract(1, status="agreed"), 2: _contract(2, status="agreed")}
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [_change(1, "supersede", replacement_contract_id=2)],
            contracts,
            "test-project"
        )
        assert len(normalized) == 1
        assert normalized[0]["destructive"] is True


# ==== Integration tests: multiple changes in one batch ====

class TestValidateBatchIntegration:
    """Integration tests: complex batches with multiple changes."""

    def test_batch_with_multiple_valid_changes(self):
        """Batch with multiple valid, independent changes."""
        contracts = {
            1: _contract(1, status="proposed", method="GET", path="/a"),
            2: _contract(2, status="agreed", method="POST", path="/b"),
            3: _contract(3, status="agreed", method="GET", path="/c"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [
                _change(1, "agree"),
                _change(2, "deprecate"),
                _change(3, "supersede", replacement_contract_id=1),
            ],
            contracts,
            "test-project"
        )
        assert len(conflicts) == 0
        assert len(normalized) == 3

    def test_batch_one_conflict_stops_other_changes(self):
        """One conflict in the batch does not prevent normalizing others."""
        contracts = {
            1: _contract(1, status="proposed"),
            2: _contract(2, status="agreed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [
                _change(1, "agree"),
                _change(999, "agree"),  # unknown contract
                _change(2, "deprecate"),
            ],
            contracts,
            "test-project"
        )
        # The unknown contract is a conflict, but the other two should still normalize
        assert len(conflicts) > 0
        assert any("unknown contract id 999" in c for c in conflicts)
        assert len(normalized) == 2  # changes 1 and 2 should normalize (best-effort)

    def test_batch_with_mixed_conflicts_and_warnings(self):
        """Batch with both conflicts (blocking) and warnings (advisory)."""
        contracts = {
            1: _contract(1, status="proposed", response_dto="", type_ref=""),
            2: _contract(2, status="proposed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [
                _change(1, "agree"),  # warning: missing canonical fields
                _change(2, "deprecate"),  # conflict: proposed -> deprecated not allowed
            ],
            contracts,
            "test-project"
        )
        # deprecate of proposed is a conflict (disallowed transition)
        assert any("not allowed" in c for c in conflicts)
        # agree with missing fields generates warning
        assert any("missing response_dto/type_ref" in w for w in warnings)

    def test_batch_creates_collision_after_multiple_changes(self):
        """Multiple changes can create a route collision."""
        contracts = {
            1: _contract(1, method="GET", path="/users/{id}", status="proposed"),
            2: _contract(2, method="GET", path="/users/:id", status="proposed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [
                _change(1, "agree"),
                _change(2, "agree"),
            ],
            contracts,
            "test-project"
        )
        # Both become active -> collision
        assert any("active duplicate for GET /users/{}" in c for c in conflicts)

    def test_batch_notification_collision_created_by_changes(self):
        """Changes to the batch can create a notification collision warning."""
        contracts = {
            1: _contract(1, path="/notifications", status="proposed"),
            2: _contract(2, path="/users/me/notifications", status="proposed"),
        }
        normalized, conflicts, warnings = lifecycle.validate_batch(
            [
                _change(1, "agree"),
                _change(2, "agree"),
            ],
            contracts,
            "test-project"
        )
        # Both become active -> warning (not a conflict, but advisory)
        assert any("notification routes overlap" in w for w in warnings)
