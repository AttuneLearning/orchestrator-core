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


# -- pull model (mode: pull | verdict) --------------------------------------- #

def _pull():
    return load_pipelines(load_settings().pipelines)["pull-1"]


def test_pipeline_one_gates_default_to_verdict_mode():
    # Back-compat: gates with no `mode` parse as verdict (engine-driven).
    assert all(g.mode == "verdict" for g in _pipeline().gates)


def test_pull_one_routes_work_to_pull_and_verdicts_to_verdict():
    p = _pull()
    assert [g.type for g in p.gates] == [
        "intake", "implementation", "verification",
        "qa_gate", "completion", "comms_response",
    ]
    modes = {g.type: g.mode for g in p.gates}
    # repo work is pull; decisions are verdict
    assert modes["implementation"] == "pull"
    assert modes["verification"] == "pull"
    assert modes["intake"] == "verdict"
    assert modes["qa_gate"] == "verdict"
    assert modes["completion"] == "verdict"
    # pull gates are owned by repo-working roles; the verdict is the lead/reviewer
    owners = {g.type: g.owner for g in p.gates}
    assert owners["implementation"] == "dev"
    assert owners["verification"] == "qa"
    assert owners["qa_gate"] == "lead"
    assert p.gate("qa_gate").on_failure == "implementation"


def test_verification_is_a_known_gate_type():
    from orchestrator.models import GateType
    assert GateType.VERIFICATION.value == "verification"


def test_pull_fe_has_an_e2e_pull_gate_before_the_verdict():
    p = load_pipelines(load_settings().pipelines)["pull-fe"]
    assert [g.type for g in p.gates] == [
        "intake", "contract_check", "implementation", "verification", "e2e",
        "qa_gate", "completion", "comms_response",
    ]
    e2e = p.gate("e2e")
    assert e2e.mode == "pull" and e2e.owner == "qa"   # QA runner executes Playwright
    assert e2e.on_failure == "implementation"
    assert next_gate(p, "e2e").type == "qa_gate"      # e2e feeds the reviewer verdict
    from orchestrator.models import GateType
    assert GateType.E2E.value == "e2e"
    # contract_check sits between intake and implementation (verdict gate, lead-owned).
    cc = p.gate("contract_check")
    assert cc.mode == "verdict" and cc.owner == "lead" and cc.on_failure == "intake"
    assert next_gate(p, "intake").type == "contract_check"
    assert next_gate(p, "contract_check").type == "implementation"
