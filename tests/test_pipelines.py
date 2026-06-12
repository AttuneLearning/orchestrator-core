"""Pure tests for pipeline / gate resolution."""

from orchestrator.config import load_settings
from orchestrator.pipelines import (
    first_gate,
    gate_applies,
    load_pipelines,
    next_gate,
)


def _pipeline():
    return load_pipelines(load_settings().pipelines)["pipeline-1"]


def test_pipeline_one_has_five_ordered_gates():
    p = _pipeline()
    assert [g.type for g in p.gates] == [
        "intake", "implementation", "qa_gate", "completion", "comms_response"
    ]
    assert [g.order for g in p.gates] == [1, 2, 3, 4, 5]


def test_qa_gate_routes_back_to_implementation_on_failure():
    p = _pipeline()
    assert p.gate("qa_gate").on_failure == "implementation"


def test_first_gate_is_intake():
    assert first_gate(_pipeline()).type == "intake"


def test_comms_response_skipped_without_message():
    p = _pipeline()
    # completion is normally followed by comms_response, but only when triggered
    nxt = next_gate(p, "completion", triggered_by_message=False)
    assert nxt is None
    nxt2 = next_gate(p, "completion", triggered_by_message=True)
    assert nxt2.type == "comms_response"


def test_gate_applies_condition():
    p = _pipeline()
    comms = p.gate("comms_response")
    assert gate_applies(comms, triggered_by_message=True) is True
    assert gate_applies(comms, triggered_by_message=False) is False
    assert gate_applies(p.gate("intake"), triggered_by_message=False) is True


def test_next_gate_normal_progression():
    p = _pipeline()
    assert next_gate(p, "intake").type == "implementation"
    assert next_gate(p, "implementation").type == "qa_gate"
    assert next_gate(p, "qa_gate").type == "completion"
