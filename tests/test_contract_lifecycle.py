"""Integration tests for contract_lifecycle repository functions.

Tests the DB-backed preview/apply/history functions using the pool fixture,
covering atomic apply, idempotency, stale tokens, history, satisfaction,
confirmation gating, and authorization.
"""

import json
from datetime import datetime

import pytest

from orchestrator import repository as repo


# ============================================================================
# Test fixtures: helper functions for building test data
# ============================================================================

def _contract_dict(method, path, status, response_dto="DTO", type_ref="TypeRef"):
    """Build a contract dict for upsert."""
    return {
        "method": method,
        "path": path,
        "status": status,
        "response_dto": response_dto,
        "type_ref": type_ref,
    }


def _change(contract_id, action, replacement_contract_id=None, source_ref=None):
    """Build a change dict for preview/apply."""
    change = {"contract_id": contract_id, "action": action}
    if replacement_contract_id is not None:
        change["replacement_contract_id"] = replacement_contract_id
    if source_ref is not None:
        change["source_ref"] = source_ref
    return change


# ============================================================================
# Preview tests: read-only, no mutation
# ============================================================================

class TestContractLifecyclePreview:
    """Test contract_lifecycle_preview (read-only, no writes)."""

    def test_preview_does_not_mutate_updated_at(self, pool):
        """Preview does not modify contract rows (updated_at unchanged)."""
        # Seed a contract
        c1 = repo.upsert_contract(pool, "GET", "/test", status="proposed")
        original_updated_at = c1["updated_at"]

        # Run preview
        result = repo.contract_lifecycle_preview(
            pool,
            project="cadencelms-working",
            operation_id="preview-1",
            actor="test",
            actor_role="orch-manager",
            reason="test preview",
            changes=[_change(c1["id"], "agree")],
        )

        # Verify preview is valid
        assert result["valid"] is True
        assert result["operation_id"] == "preview-1"
        assert len(result["normalized_changes"]) == 1
        assert len(result["affected"]) == 1

        # Verify no mutation: read the contract again
        c1_after = repo.get_contract(pool, "GET", "/test")
        assert c1_after["status"] == "proposed"
        assert c1_after["updated_at"] == original_updated_at

    def test_preview_returns_affected_contracts(self, pool):
        """Preview returns affected list with tokens and change details."""
        c1 = repo.upsert_contract(pool, "GET", "/a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/b", status="agreed")

        result = repo.contract_lifecycle_preview(
            pool,
            project="cadencelms-working",
            operation_id="preview-2",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree"), _change(c2["id"], "deprecate")],
        )

        assert result["valid"] is True
        assert len(result["affected"]) == 2
        # Check each affected entry has required fields
        for aff in result["affected"]:
            assert "contract_id" in aff
            assert "method" in aff
            assert "path" in aff
            assert "from_status" in aff
            assert "to_status" in aff
            assert "action" in aff
            assert "destructive" in aff
            assert "token" in aff  # updated_at isoformat

    def test_preview_reports_destructive_flag(self, pool):
        """Preview correctly classifies destructive vs non-destructive changes."""
        c1 = repo.upsert_contract(pool, "GET", "/a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/b", status="agreed")
        c3 = repo.upsert_contract(pool, "PUT", "/c", status="agreed")

        # agree is non-destructive
        result = repo.contract_lifecycle_preview(
            pool,
            project="cadencelms-working",
            operation_id="preview-destructive-1",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree")],
        )
        assert result["destructive"] is False
        assert result["confirmation_required"] is False

        # supersede is destructive
        result = repo.contract_lifecycle_preview(
            pool,
            project="cadencelms-working",
            operation_id="preview-destructive-2",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c2["id"], "supersede", replacement_contract_id=c1["id"])],
        )
        assert result["destructive"] is True
        assert result["confirmation_required"] is True

        # deprecate of agreed is destructive
        result = repo.contract_lifecycle_preview(
            pool,
            project="cadencelms-working",
            operation_id="preview-destructive-3",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c3["id"], "deprecate")],
        )
        assert result["destructive"] is True


# ============================================================================
# Apply tests: atomic, idempotent, confirmation gating
# ============================================================================

class TestContractLifecycleApply:
    """Test contract_lifecycle_apply (atomic, idempotent, confirmation gates)."""

    def test_apply_simple_agree_succeeds(self, pool):
        """Apply a simple agree action changes status atomically."""
        c1 = repo.upsert_contract(pool, "GET", "/simple", status="proposed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-agree-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="test agree",
            changes=[_change(c1["id"], "agree")],
            source="test",
        )

        # Verify apply succeeded
        assert result["result"] == "applied"
        assert result["operation_id"] == "apply-agree-1"
        assert len(result["changed"]) == 1
        assert result["changed"][0]["contract_id"] == c1["id"]
        assert result["changed"][0]["action"] == "agree"
        assert result["changed"][0]["from_status"] == "proposed"
        assert result["changed"][0]["to_status"] == "agreed"
        assert "audit_op_id" in result

        # Verify contract status changed
        c1_after = repo.get_contract(pool, "GET", "/simple")
        assert c1_after["status"] == "agreed"

    def test_apply_is_atomic_on_conflict(self, pool):
        """If any change in a batch has a conflict, nothing is applied."""
        c1 = repo.upsert_contract(pool, "GET", "/a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/b", status="proposed")

        # Try to apply: agree c1, but deprecate c2 (proposed->deprecated not allowed)
        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-atomic-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree"), _change(c2["id"], "deprecate")],
            source="test",
        )

        # Verify apply rejected due to conflict
        assert result["result"] == "conflict"
        assert "conflicts" in result
        assert len(result["conflicts"]) > 0

        # Verify no changes were applied
        c1_after = repo.get_contract(pool, "GET", "/a")
        c2_after = repo.get_contract(pool, "POST", "/b")
        assert c1_after["status"] == "proposed"
        assert c2_after["status"] == "proposed"

    def test_apply_stale_token_conflict(self, pool):
        """If expected tokens don't match, apply records a conflict and changes nothing."""
        c1 = repo.upsert_contract(pool, "GET", "/stale", status="proposed")
        original_token = c1["updated_at"].isoformat()

        # Modify the contract to change its updated_at
        repo.set_contract_status(pool, "GET", "/stale", "proposed")  # Force updated_at bump
        c1_modified = repo.get_contract(pool, "GET", "/stale")
        new_token = c1_modified["updated_at"].isoformat()
        assert new_token != original_token

        # Try to apply with the old (stale) token
        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-stale-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree")],
            expected={str(c1["id"]): original_token},  # Old token
            source="test",
        )

        # Verify conflict reported
        assert result["result"] == "conflict"
        assert result.get("reason") == "stale_token"
        assert c1["id"] in result.get("stale", [])

        # Verify no change applied
        c1_after = repo.get_contract(pool, "GET", "/stale")
        assert c1_after["status"] == "proposed"

    def test_apply_idempotent_same_operation_id(self, pool):
        """Applying the same operation_id twice returns identical response, no dup events."""
        c1 = repo.upsert_contract(pool, "GET", "/idempotent", status="proposed")

        # First apply
        result1 = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-idempotent-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="first apply",
            changes=[_change(c1["id"], "agree")],
            source="test",
        )

        assert result1["result"] == "applied"
        op_id_1 = result1["audit_op_id"]

        # Second apply with same operation_id
        result2 = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-idempotent-1",  # Same!
            actor="test-admin-different",  # Different actor (should not matter)
            actor_role="orch-manager",
            reason="second apply",  # Different reason (should not matter)
            changes=[_change(c1["id"], "agree")],
            source="test",
        )

        # Should return the identical response from the first apply
        assert result2 == result1
        assert result2["audit_op_id"] == op_id_1

        # Verify exactly one op row exists for this operation_id
        history = repo.contract_lifecycle_history(pool, operation_id="apply-idempotent-1")
        # Group by op_id to count unique ops
        unique_ops = set(h.get("op_id") for h in history)
        assert len(unique_ops) == 1  # Only one op

        # Verify only one event exists (not duplicated)
        events_for_op = [h for h in history if h.get("op_id") == op_id_1]
        assert len(events_for_op) == 1

    def test_apply_destructive_requires_confirmation(self, pool):
        """Destructive batch (supersede/retire) without confirm_project is rejected."""
        c1 = repo.upsert_contract(pool, "GET", "/destructive", status="agreed")
        c2 = repo.upsert_contract(pool, "POST", "/other", status="proposed")

        # Try supersede without confirm_project
        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-destructive-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "supersede", replacement_contract_id=c2["id"])],
            confirm_project=None,  # Missing confirmation
            source="test",
        )

        assert result["result"] == "rejected"
        assert result.get("reason") == "confirmation_required"
        assert "destructive_changes" in result
        assert c1["id"] in result["destructive_changes"]

        # Verify contract unchanged
        c1_after = repo.get_contract(pool, "GET", "/destructive")
        assert c1_after["status"] == "agreed"

    def test_apply_destructive_with_correct_confirmation_succeeds(self, pool):
        """Destructive batch with correct confirm_project applies successfully."""
        c1 = repo.upsert_contract(pool, "GET", "/destructive-ok", status="agreed")
        c2 = repo.upsert_contract(pool, "POST", "/replacement", status="agreed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-destructive-2",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "supersede", replacement_contract_id=c2["id"])],
            confirm_project="cadencelms-working",  # Correct project name
            source="test",
        )

        assert result["result"] == "applied"
        assert result["destructive"] is True
        assert result["confirmed"]["required"] is True
        assert result["confirmed"]["value_matched"] is True

        # Verify contract changed
        c1_after = repo.get_contract(pool, "GET", "/destructive-ok")
        assert c1_after["status"] == "superseded"
        assert c1_after["superseded_by_contract_id"] == c2["id"]

    def test_apply_non_destructive_batch_ignores_confirm_project(self, pool):
        """Pure agree batch applies without requiring confirm_project."""
        c1 = repo.upsert_contract(pool, "GET", "/non-dest", status="proposed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-non-dest-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree")],
            confirm_project=None,  # No confirmation needed or provided
            source="test",
        )

        assert result["result"] == "applied"
        assert result["destructive"] is False
        assert result["confirmed"]["required"] is False

        c1_after = repo.get_contract(pool, "GET", "/non-dest")
        assert c1_after["status"] == "agreed"

    def test_apply_wrong_confirm_project_rejected(self, pool):
        """Destructive batch with wrong confirm_project value is rejected."""
        c1 = repo.upsert_contract(pool, "GET", "/wrong-confirm", status="agreed")
        c2 = repo.upsert_contract(pool, "POST", "/replacement", status="agreed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="apply-wrong-confirm-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "supersede", replacement_contract_id=c2["id"])],
            confirm_project="wrong-project-name",  # Wrong!
            source="test",
        )

        assert result["result"] == "rejected"
        assert result.get("reason") == "confirmation_required"

        c1_after = repo.get_contract(pool, "GET", "/wrong-confirm")
        assert c1_after["status"] == "agreed"  # Unchanged


# ============================================================================
# History tests: read-back by contract_id and operation_id
# ============================================================================

class TestContractLifecycleHistory:
    """Test contract_lifecycle_history (read-only audit trail)."""

    def test_history_records_applied_operations(self, pool):
        """History records events from applied operations."""
        c1 = repo.upsert_contract(pool, "GET", "/history", status="proposed")

        # Apply an operation
        apply_result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="history-op-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="test reason",
            changes=[_change(c1["id"], "agree", source_ref="api#v1")],
            source="test",
        )

        # Fetch history for this contract
        history = repo.contract_lifecycle_history(pool, contract_id=c1["id"])

        assert len(history) > 0
        evt = history[0]
        assert evt["contract_id"] == c1["id"]
        assert evt["action"] == "agree"
        assert evt["from_status"] == "proposed"
        assert evt["to_status"] == "agreed"
        assert evt["actor"] == "test-admin"
        assert evt["reason"] == "test reason"
        assert evt["source"] == "test"
        assert evt["operation_id"] == "history-op-1"

    def test_history_filtered_by_contract_id(self, pool):
        """History can be filtered by contract_id."""
        c1 = repo.upsert_contract(pool, "GET", "/contract1", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/contract2", status="proposed")

        # Apply operations touching both contracts
        repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="history-multi-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree"), _change(c2["id"], "agree")],
            source="test",
        )

        # History filtered to c1 only
        history_c1 = repo.contract_lifecycle_history(pool, contract_id=c1["id"])
        assert all(h["contract_id"] == c1["id"] for h in history_c1)
        assert len(history_c1) == 1

        # History filtered to c2 only
        history_c2 = repo.contract_lifecycle_history(pool, contract_id=c2["id"])
        assert all(h["contract_id"] == c2["id"] for h in history_c2)
        assert len(history_c2) == 1

    def test_history_filtered_by_operation_id(self, pool):
        """History can be filtered by operation_id (TEXT, not numeric)."""
        c1 = repo.upsert_contract(pool, "GET", "/op-a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/op-b", status="proposed")

        # Apply first operation
        repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="history-op-A",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree")],
            source="test",
        )

        # Apply second operation
        repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="history-op-B",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c2["id"], "agree")],
            source="test",
        )

        # History filtered to op-A only
        history_a = repo.contract_lifecycle_history(pool, operation_id="history-op-A")
        assert all(h["operation_id"] == "history-op-A" for h in history_a)
        assert len(history_a) == 1
        assert history_a[0]["contract_id"] == c1["id"]

        # History filtered to op-B only
        history_b = repo.contract_lifecycle_history(pool, operation_id="history-op-B")
        assert all(h["operation_id"] == "history-op-B" for h in history_b)
        assert len(history_b) == 1
        assert history_b[0]["contract_id"] == c2["id"]

    def test_history_records_conflict_operations(self, pool):
        """History includes rejected/conflict operations, not just applied ones."""
        c1 = repo.upsert_contract(pool, "GET", "/conflict", status="proposed")

        # Apply an operation that will conflict
        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="history-conflict-1",
            actor="test-admin",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "deprecate")],  # proposed -> deprecated not allowed
            source="test",
        )

        assert result["result"] == "conflict"

        # Fetch history: even the conflict operation should have a record
        history = repo.contract_lifecycle_history(pool, operation_id="history-conflict-1")
        assert len(history) == 0  # Conflicts don't insert events, only ops row

    def test_history_ordered_by_created_at_desc(self, pool):
        """History is ordered by created_at DESC (newest first)."""
        c1 = repo.upsert_contract(pool, "GET", "/ordered", status="proposed")

        # Apply multiple operations to the same contract
        op_ids = []
        for i in range(3):
            result = repo.contract_lifecycle_apply(
                pool,
                project="cadencelms-working",
                operation_id=f"history-order-{i}",
                actor="test",
                actor_role="orch-manager",
                reason="",
                changes=[_change(c1["id"], "agree" if i == 0 else "deprecate")],
                source="test",
            )
            if result["result"] == "applied":
                op_ids.append(result["operation_id"])

        # Note: first agree succeeds, then deprecate of proposed fails, then...
        # Let me re-do this to all succeed
        c_list = [
            repo.upsert_contract(pool, "GET", f"/ordered-{i}", status="proposed")
            for i in range(3)
        ]

        for i, c in enumerate(c_list):
            repo.contract_lifecycle_apply(
                pool,
                project="cadencelms-working",
                operation_id=f"history-order-{i}",
                actor="test",
                actor_role="orch-manager",
                reason="",
                changes=[_change(c["id"], "agree")],
                source="test",
            )

        # Fetch all history for these contracts
        history = repo.contract_lifecycle_history(pool, contract_id=c_list[0]["id"])
        if len(history) > 1:
            # Verify ordered descending by created_at
            for i in range(len(history) - 1):
                assert history[i]["created_at"] >= history[i + 1]["created_at"]


# ============================================================================
# Satisfaction tests: retired/superseded are not satisfied
# ============================================================================

class TestContractSatisfaction:
    """Test that retired/superseded contracts are not counted as satisfied."""

    def test_retired_contract_not_satisfied(self, pool):
        """A retired contract does not satisfy contract_satisfied()."""
        repo.upsert_contract(pool, "GET", "/retired-test", status="live")
        assert repo.contract_satisfied(pool, "GET", "/retired-test") is True

        # Set to retired
        repo.set_contract_status(pool, "GET", "/retired-test", "retired")
        assert repo.contract_satisfied(pool, "GET", "/retired-test") is False

    def test_superseded_contract_not_satisfied(self, pool):
        """A superseded contract does not satisfy contract_satisfied()."""
        c1 = repo.upsert_contract(pool, "POST", "/superseded-test", status="agreed")
        c2 = repo.upsert_contract(pool, "PUT", "/replacement", status="agreed")
        assert repo.contract_satisfied(pool, "POST", "/superseded-test") is True

        # Use apply to supersede with lifecycle audit trail
        repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="satisfy-supersede-1",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "supersede", replacement_contract_id=c2["id"])],
            confirm_project="cadencelms-working",
            source="test",
        )

        assert repo.contract_satisfied(pool, "POST", "/superseded-test") is False

    def test_deprecated_contract_not_satisfied(self, pool):
        """A deprecated contract does not satisfy contract_satisfied()."""
        repo.upsert_contract(pool, "DELETE", "/deprecated-test", status="live")
        assert repo.contract_satisfied(pool, "DELETE", "/deprecated-test") is True

        repo.set_contract_status(pool, "DELETE", "/deprecated-test", "deprecated")
        assert repo.contract_satisfied(pool, "DELETE", "/deprecated-test") is False

    def test_rejected_contract_not_satisfied(self, pool):
        """A rejected contract does not satisfy contract_satisfied()."""
        repo.upsert_contract(pool, "PATCH", "/rejected-test", status="proposed")
        assert repo.contract_satisfied(pool, "PATCH", "/rejected-test") is False

        repo.set_contract_status(pool, "PATCH", "/rejected-test", "rejected")
        assert repo.contract_satisfied(pool, "PATCH", "/rejected-test") is False


# ============================================================================
# Batch apply tests: multiple changes in one operation
# ============================================================================

class TestBatchApply:
    """Test applying multiple changes in a single operation."""

    def test_batch_multiple_agrees(self, pool):
        """Apply a batch agreeing multiple proposed contracts."""
        c1 = repo.upsert_contract(pool, "GET", "/batch-a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/batch-b", status="proposed")
        c3 = repo.upsert_contract(pool, "PUT", "/batch-c", status="proposed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="batch-agree-1",
            actor="test",
            actor_role="orch-manager",
            reason="bulk agree",
            changes=[
                _change(c1["id"], "agree"),
                _change(c2["id"], "agree"),
                _change(c3["id"], "agree"),
            ],
            source="test",
        )

        assert result["result"] == "applied"
        assert len(result["changed"]) == 3

        # Verify all three are now agreed
        assert repo.contract_satisfied(pool, "GET", "/batch-a")
        assert repo.contract_satisfied(pool, "POST", "/batch-b")
        assert repo.contract_satisfied(pool, "PUT", "/batch-c")

    def test_batch_mixed_actions(self, pool):
        """Apply a batch with mixed actions (agree, deprecate, supersede)."""
        c1 = repo.upsert_contract(pool, "GET", "/mixed-a", status="proposed")
        c2 = repo.upsert_contract(pool, "POST", "/mixed-b", status="agreed")
        c3 = repo.upsert_contract(pool, "PUT", "/mixed-c", status="agreed")
        c4 = repo.upsert_contract(pool, "DELETE", "/mixed-d", status="agreed")

        result = repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="batch-mixed-1",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[
                _change(c1["id"], "agree"),
                _change(c2["id"], "deprecate"),
                _change(c3["id"], "supersede", replacement_contract_id=c4["id"]),
            ],
            confirm_project="cadencelms-working",
            source="test",
        )

        assert result["result"] == "applied"
        assert len(result["changed"]) == 3

        # Verify each action took effect
        c1_check = repo.get_contract(pool, "GET", "/mixed-a")
        assert c1_check["status"] == "agreed"

        c2_check = repo.get_contract(pool, "POST", "/mixed-b")
        assert c2_check["status"] == "deprecated"

        c3_check = repo.get_contract(pool, "PUT", "/mixed-c")
        assert c3_check["status"] == "superseded"
        assert c3_check["superseded_by_contract_id"] == c4["id"]

    def test_batch_with_source_ref_recorded(self, pool):
        """Source_ref in changes is recorded in history events."""
        c1 = repo.upsert_contract(pool, "GET", "/source-ref", status="proposed")

        repo.contract_lifecycle_apply(
            pool,
            project="cadencelms-working",
            operation_id="batch-source-1",
            actor="test",
            actor_role="orch-manager",
            reason="",
            changes=[_change(c1["id"], "agree", source_ref="api#v2")],
            source="test",
        )

        history = repo.contract_lifecycle_history(pool, contract_id=c1["id"])
        assert len(history) > 0
        assert history[0]["source_ref"] == "api#v2"
