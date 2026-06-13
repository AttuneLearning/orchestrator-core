"""Pipeline / gate resolution. Pure and table-driven — no database access.

A pipeline is an ordered list of gates loaded from config/pipelines.yaml. An
issue's `gate_type` names the gate it is currently working. Gates carrying a
`condition` are skipped when the condition does not hold for the issue (e.g.
comms_response is skipped unless the issue was triggered by an inbound message).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Gate:
    type: str
    order: int
    owner: str = "dev"
    description: str = ""
    condition: Optional[str] = None
    on_failure: Optional[str] = None
    exit_criteria: tuple[str, ...] = field(default_factory=tuple)
    # How the gate's work is performed:
    #   "verdict" (default) — the gate owner renders a decision over state the
    #     orchestrator already holds (reasoner gate_review, a human, or a
    #     delegated reviewer). The engine drives it in-process.
    #   "pull" — a registered external worker of `owner` claims the issue and
    #     does the work in its own repo, reporting back via MCP. The engine
    #     assigns + observes but never runs a worker on it.
    mode: str = "verdict"


@dataclass(frozen=True)
class Pipeline:
    id: str
    name: str
    description: str
    gates: tuple[Gate, ...]
    # The team that owns every issue decomposed under this pipeline. When set,
    # decomposition inherits it (children never re-derive team from issue text —
    # that text inference is what misroutes pull-fe work to backend). None = fall
    # back to the reasoner's per-issue team.
    team: Optional[str] = None

    def gate(self, gate_type: str) -> Optional[Gate]:
        for g in self.gates:
            if g.type == gate_type:
                return g
        return None


def load_pipelines(config: dict[str, Any]) -> dict[str, Pipeline]:
    """Parse the raw pipelines.yaml dict into Pipeline objects."""
    out: dict[str, Pipeline] = {}
    for pid, spec in (config.get("pipelines") or {}).items():
        gates = tuple(
            Gate(
                type=g["type"],
                order=g["order"],
                owner=g.get("owner", "dev"),
                description=g.get("description", ""),
                condition=g.get("condition"),
                on_failure=g.get("on_failure"),
                exit_criteria=tuple(g.get("exit_criteria", []) or []),
                mode=g.get("mode", "verdict"),
            )
            for g in sorted(spec.get("gates", []), key=lambda x: x["order"])
        )
        out[pid] = Pipeline(
            id=pid,
            name=spec.get("name", pid),
            description=spec.get("description", ""),
            gates=gates,
            team=spec.get("team"),
        )
    return out


def gate_applies(gate: Gate, *, triggered_by_message: bool) -> bool:
    """Whether a conditional gate is in scope for an issue."""
    if gate.condition == "triggered_by_message":
        return triggered_by_message
    return True


def first_gate(pipeline: Pipeline, *, triggered_by_message: bool = False) -> Optional[Gate]:
    for g in pipeline.gates:
        if gate_applies(g, triggered_by_message=triggered_by_message):
            return g
    return None


def next_gate(
    pipeline: Pipeline, current_gate_type: str, *, triggered_by_message: bool = False
) -> Optional[Gate]:
    """The next applicable gate after current_gate_type, or None if it was last."""
    seen = False
    for g in pipeline.gates:
        if seen and gate_applies(g, triggered_by_message=triggered_by_message):
            return g
        if g.type == current_gate_type:
            seen = True
    return None
