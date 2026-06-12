"""Pure tests for the issue state machine."""

from orchestrator.config import load_settings
from orchestrator.pipelines import load_pipelines
from orchestrator.state_machine import apply_gate_decision, validate_transition


def _pipeline():
    return load_pipelines(load_settings().pipelines)["pipeline-1"]


# --- transition validity ---------------------------------------------------- #

def test_legal_transitions():
    assert validate_transition("ready", "in_progress")
    assert validate_transition("in_progress", "in_review")
    assert validate_transition("in_review", "done")
    assert validate_transition("in_review", "in_progress")


def test_illegal_transitions():
    assert not validate_transition("done", "in_progress")
    assert not validate_transition("backlog", "done")
    assert not validate_transition("failed", "ready")


def test_off_rails_reachable_from_active_only():
    assert validate_transition("in_progress", "off_rails")
    assert validate_transition("in_review", "off_rails")
    assert not validate_transition("done", "off_rails")
    assert not validate_transition("off_rails", "ready")  # latched


# --- gate decisions --------------------------------------------------------- #

def test_pass_advances_to_next_gate():
    p = _pipeline()
    out = apply_gate_decision(
        p, p.gate("intake"), passed=True, retry_count=0, retry_cap=3
    )
    assert out.state == "in_progress"
    assert out.gate_type == "implementation"
    assert out.event_type == "gate_pass"


def test_pass_on_last_applicable_gate_completes():
    p = _pipeline()
    # completion is the last gate when not triggered by a message
    out = apply_gate_decision(
        p, p.gate("completion"), passed=True, retry_count=0, retry_cap=3,
        triggered_by_message=False,
    )
    assert out.state == "done"
    assert out.gate_type is None


def test_pass_on_completion_advances_to_comms_when_triggered():
    p = _pipeline()
    out = apply_gate_decision(
        p, p.gate("completion"), passed=True, retry_count=0, retry_cap=3,
        triggered_by_message=True,
    )
    assert out.state == "in_progress"
    assert out.gate_type == "comms_response"


def test_decline_increments_retry_and_redoes_gate():
    p = _pipeline()
    out = apply_gate_decision(
        p, p.gate("implementation"), passed=False, retry_count=0, retry_cap=3
    )
    assert out.state == "in_progress"
    assert out.gate_type == "implementation"
    assert out.retry_count == 1
    assert out.event_type == "gate_decline"


def test_qa_decline_routes_back_to_implementation():
    p = _pipeline()
    out = apply_gate_decision(
        p, p.gate("qa_gate"), passed=False, retry_count=0, retry_cap=3
    )
    assert out.gate_type == "implementation"
    assert out.retry_count == 1


def test_decline_at_retry_cap_fails():
    p = _pipeline()
    out = apply_gate_decision(
        p, p.gate("implementation"), passed=False, retry_count=2, retry_cap=3
    )
    assert out.state == "failed"
    assert out.retry_count == 3
