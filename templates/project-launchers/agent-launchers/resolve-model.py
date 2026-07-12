#!/usr/bin/env python3
"""Resolve a launcher -m/--model selection against agent-model.yaml.

Usage:
    resolve-model.py <harness> <shortcut-or-model>   # -> prints model string
    resolve-model.py <harness> --default             # -> prints default model string
    resolve-model.py <harness> --list                # -> table for one harness
    resolve-model.py --list                          # -> table for all harnesses

On an unknown harness/model it prints the valid options to stderr and exits 2,
so launchers can fail fast with a helpful message.
"""

from __future__ import annotations

import os
import sys


def _find_yaml() -> str:
    candidates = [
        os.environ.get("AGENT_MODEL_YAML"),
        os.path.join(os.environ["WORKSPACE_ROOT"], "agent-model.yaml")
        if os.environ.get("WORKSPACE_ROOT") else None,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent-model.yaml"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("agent-model.yaml not found (set AGENT_MODEL_YAML or WORKSPACE_ROOT)")


def _load() -> dict:
    import yaml  # PyYAML (present in the orchestrator venv)
    with open(_find_yaml(), encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("harnesses", {}) or {}


def _fmt_table(harnesses: dict, only: str | None = None) -> str:
    lines = []
    for hname, h in harnesses.items():
        if only and hname != only:
            continue
        prov = h.get("provider", "")
        default = h.get("default", "")
        lines.append(f"{hname}  (provider={prov}, default={default})")
        for shortcut, spec in (h.get("models") or {}).items():
            spec = spec or {}
            model = spec.get("model", "")
            note = spec.get("note", "")
            star = " *" if shortcut == default else ""
            lines.append(f"    {shortcut:<16} -> {model:<28} {note}{star}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[0] == "--list":
        print(_fmt_table(_load()))
        return 0

    harness = argv[0]
    harnesses = _load()
    if harness not in harnesses:
        print(f"unknown harness {harness!r}. Known: {', '.join(harnesses)}", file=sys.stderr)
        return 2
    h = harnesses[harness]
    models = h.get("models") or {}

    sel = argv[1] if len(argv) > 1 else "--default"
    if sel == "--list":
        print(_fmt_table(harnesses, only=harness))
        return 0
    if sel == "--default":
        sel = h.get("default")
        if not sel:
            print(f"no default model for harness {harness!r}", file=sys.stderr)
            return 2

    # exact shortcut match
    if sel in models:
        print((models[sel] or {}).get("model", sel))
        return 0
    # raw model-string that is already listed for this harness
    known_strings = {(spec or {}).get("model") for spec in models.values()}
    if sel in known_strings:
        print(sel)
        return 0
    # HARD ERROR: not a valid (harness, model) combination.
    print(
        f"'{sel}' is not a valid model for harness '{harness}'.\n"
        f"Valid options (shortcut -> model):",
        file=sys.stderr,
    )
    print(_fmt_table(harnesses, only=harness), file=sys.stderr)
    print(
        f"\nRefresh the list from live provider endpoints with:\n"
        f"    ./gather-models.sh            # all providers you have keys for\n"
        f"    agent-launchers/gather-models.py {harness}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
