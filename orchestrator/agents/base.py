"""Shared agent types and helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class IssueSpec:
    """A proposed issue produced by goal decomposition."""

    title: str
    description: str = ""
    team: str = "backend"


@dataclass
class GateReview:
    passed: bool
    reasons: list[str]


@dataclass
class CodeResult:
    """Output of the code-generation leg. Stored, never executed."""

    content: str
    provider: str
    model: str = ""


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM text response.

    Accepts raw JSON, ```json fenced blocks, or the first {...}/[...] span.
    Raises ValueError if nothing parseable is found.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    raise ValueError(f"no JSON found in response: {text[:200]!r}")
