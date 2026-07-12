"""Pure tests for the reasoner backends — no DB, no network.

Exercises the shared _LLMReasoner ops via a fake _ask, and make_reasoner backend
selection (REASONER override + auto).
"""

import copy

import pytest

from orchestrator.config import Settings
from orchestrator.agents import reasoning
from orchestrator.agents.reasoning import (
    AnthropicReasoner, CliReasoner, OpenAIReasoner, ReasonerExhausted,
    StubReasoner, _LLMReasoner, _is_transient, make_reasoner,
)
from orchestrator.models import Goal, Issue


class _Fake(_LLMReasoner):
    """An _LLMReasoner whose model call returns a canned string."""
    def __init__(self, resp: str):
        self._resp = resp

    def _ask(self, system, user, max_tokens=1024):
        return self._resp


_GOAL = Goal(id=1, title="Add UI spinner", description="loading indicator")
_ISSUE = Issue(id=1, goal_id=1, title="Spinner", description="render a spinner")


# -- shared ops parse model output correctly -------------------------------- #

def test_decompose_goal_routes_team_from_model_output():
    specs = _Fake('[{"title":"A","team":"frontend"},{"title":"B"}]').decompose_goal(_GOAL, 5)
    assert [(s.title, s.team) for s in specs] == [("A", "frontend"), ("B", "backend")]


def test_gate_review_parses_decline_with_violations():
    r = _Fake('{"passed": false, "reasons": ["nope"], "violated_rules": ["ADR-UI-001"]}')
    review = r.gate_review(_ISSUE, "qa_gate", recent=[], rules="- [ADR-UI-001] x")
    assert review.passed is False
    assert review.violated_rules == ["ADR-UI-001"]


def test_score_drift_parses_float():
    assert _Fake('{"drift_score": 0.4}').score_drift(_ISSUE, recent=[]) == pytest.approx(0.4)


def test_triage_and_complexity_and_adr():
    assert _Fake('{"accept": true, "title": "T"}').triage_message(
        {"from_team": "a", "to_team": "b", "subject": "s"}).accept is True
    ca = _Fake('{"decompose": true, "subissues": [{"title": "s1"}]}').assess_complexity(_ISSUE)
    assert ca.decompose is True and ca.subissues[0].title == "s1"
    assert _Fake('null').suggest_adr(_ISSUE) is None
    assert _Fake('{"domain":"UI","title":"t","decision":"do x"}').suggest_adr(_ISSUE)["decision"] == "do x"


def test_fenced_json_is_tolerated():
    # extract_json handles ```json fences from chatty models
    specs = _Fake('```json\n[{"title":"A"}]\n```').decompose_goal(_GOAL, 5)
    assert specs[0].title == "A"


# -- make_reasoner backend selection ---------------------------------------- #

def _settings(**kw) -> Settings:
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_make_reasoner_auto_defaults_to_stub_without_key():
    assert isinstance(make_reasoner(_settings()), StubReasoner)


def test_make_reasoner_auto_anthropic_with_key():
    assert isinstance(make_reasoner(_settings(anthropic_api_key="sk-x")), AnthropicReasoner)


def test_make_reasoner_explicit_overrides():
    assert isinstance(make_reasoner(_settings(reasoner="stub", anthropic_api_key="sk-x")),
                      StubReasoner)
    assert isinstance(make_reasoner(_settings(reasoner="cli")), CliReasoner)
    assert isinstance(
        make_reasoner(_settings(reasoner="openai", reasoner_base_url="http://localhost:8081/v1",
                                reasoner_model="qwen")),
        OpenAIReasoner)


def test_make_reasoner_unknown_provider_raises():
    with pytest.raises(ValueError):
        make_reasoner(_settings(reasoner="bogus"))


# -- OpenAIReasoner overload resilience (retry / fallback / exhaustion) ------ #

class _HttpErr(Exception):
    """Stand-in for an openai SDK error carrying an HTTP status."""
    def __init__(self, status: int, msg: str = "boom"):
        super().__init__(msg)
        self.status_code = status


def test_is_transient_classification():
    assert _is_transient(_HttpErr(429)) is True          # rate limit
    assert _is_transient(_HttpErr(503)) is True          # unavailable
    assert _is_transient(_HttpErr(529)) is True          # overloaded
    assert _is_transient(_HttpErr(400)) is False         # bad request
    assert _is_transient(_HttpErr(401)) is False         # auth
    assert _is_transient(Exception("Model is overloaded, try again")) is True
    assert _is_transient(Exception("connection reset by peer")) is True
    assert _is_transient(ValueError("bad json")) is False


def _oai(**kw) -> OpenAIReasoner:
    """OpenAIReasoner with tiny counts; _call is replaced per-test so no network
    and _sleep is neutralized by the fixture below."""
    base = dict(reasoner="openai", reasoner_base_url="http://x/v1",
                reasoner_model="glm", reasoner_fallback_model="ds",
                reasoner_retries=2, reasoner_path_cycles=2,
                reasoner_backoff_base=0.0, reasoner_path_pause_s=0.0)
    base.update(kw)
    return OpenAIReasoner(_settings(**base))


@pytest.fixture()
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(reasoning, "_sleep", lambda s: calls.append(s))
    return calls


def test_openai_retries_then_succeeds_on_primary(no_sleep):
    r = _oai()
    seq = [_HttpErr(503), "ok"]     # first attempt transient-fails, second works
    def fake(model, system, user, max_tokens):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    r._call = fake
    assert r.plan_issue(_ISSUE) == "ok"
    assert len(no_sleep) == 1        # one backoff between the two attempts


def test_openai_falls_back_to_second_model_when_primary_overloaded(no_sleep):
    r = _oai()
    used = []
    def fake(model, system, user, max_tokens):
        used.append(model)
        if model == "glm":
            raise _HttpErr(529)      # primary always overloaded
        return "fallback-answer"
    r._call = fake
    assert r.plan_issue(_ISSUE) == "fallback-answer"
    assert used[0] == "glm" and "ds" in used   # tried primary first, then fallback


def test_openai_raises_reasoner_exhausted_after_all_cycles(no_sleep):
    r = _oai()
    calls = {"n": 0}
    def fake(model, system, user, max_tokens):
        calls["n"] += 1
        raise _HttpErr(503)          # everything stays down
    r._call = fake
    with pytest.raises(ReasonerExhausted):
        r.plan_issue(_ISSUE)
    # 2 models * 2 retries * 2 cycles = 8 attempts; a path pause between cycles.
    assert calls["n"] == 8
    assert no_sleep.count(0.0) >= 1  # at least the inter-cycle pause happened


def test_openai_permanent_error_raises_immediately_no_retry(no_sleep):
    r = _oai()
    calls = {"n": 0}
    def fake(model, system, user, max_tokens):
        calls["n"] += 1
        raise _HttpErr(400, "bad request")   # deterministic client error
    r._call = fake
    with pytest.raises(_HttpErr):
        r.plan_issue(_ISSUE)
    assert calls["n"] == 1           # no retry, no fallback, no ReasonerExhausted
    assert no_sleep == []


def test_cli_reasoner_builds_argv_from_prompt(monkeypatch):
    """CliReasoner formats {prompt} into the command and parses stdout (no real CLI)."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = '{"drift_score": 0.9}'
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return _Proc()

    monkeypatch.setattr(reasoning.subprocess, "run", fake_run)
    r = CliReasoner(_settings(reasoner_cli_cmd='claude -p "{prompt}"'))
    assert r.score_drift(_ISSUE, recent=[]) == pytest.approx(0.9)
    assert captured["argv"][:2] == ["claude", "-p"]
    assert "Spinner" in captured["argv"][2]      # combined prompt passed as one arg
    assert captured["cwd"]                        # runs in a scratch cwd
