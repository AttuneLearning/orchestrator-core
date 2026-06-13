"""Build the orchestration-monitor knowledge base.

The /orch/monitor draft agent answers "how does the orchestration process work"
questions, so it must be grounded in the tool's own source of truth. This module
collects that truth — migration DDL, IMPLEMENTATION_SUMMARY.md, MCP tool
docstrings, the public repository API, config, and an authored contract-store
spec — and writes it as notes into the reserved, isolated `monitor:kb` memory
scope (see repository._scope_filter: that namespace is private to the monitor).

Idempotent: build_monitor_kb wipes the scope and rebuilds, so re-running it after
a code update refreshes the KB to the current source. Used by the
`ingest-monitor-kb` CLI and the auto-bootstrap-if-empty hooks (server/dashboard/migrate).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Optional

from . import repository as repo
from .config import CONFIG_DIR, MIGRATIONS_DIR, REPO_ROOT, Settings
from .embeddings import make_embedder

MONITOR_KB_SCOPE = "monitor:kb"

# Authored ground truth for the question that exposed the gap: the contract-store
# ingestion format the `import-contracts` CLI / repository.upsert_contract expect.
_CONTRACT_SPEC_NOTE = (
    "[contract-store] Seed format for the API contract store (CLI `import-contracts "
    "<file>`): a FLAT JSON ARRAY (no envelope, no _meta), one object per endpoint, "
    "idempotent/keyed on (method, path). Each object: {method, path, request_ref "
    "(zod validator export name), response_dto (DTO type name), auth, owner_team, "
    "status ('live' if wired in routes else 'proposed'), version, source_ref}. "
    "Validators and DTOs are referenced BY NAME, not inlined. content_hash is "
    "computed by the importer. Contract is 'satisfied' when status is agreed or live; "
    "the pull-fe contract_check gate blocks frontend new-endpoint issues until the "
    "endpoints they consume are satisfied (see migration 0011)."
)


def _migration_notes() -> list[str]:
    notes = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text()
        # The leading `-- ...` comment block is the human-written intent.
        header = [ln[2:].strip() for ln in text.splitlines()
                  if ln.strip().startswith("--") and "===" not in ln]
        desc = " ".join(h for h in header if h) or text[:400]
        notes.append(f"[schema:{path.name}] {desc}")
    return notes


def _impl_summary_notes() -> list[str]:
    path = REPO_ROOT / "IMPLEMENTATION_SUMMARY.md"
    if not path.exists():
        return []
    notes, title, buf = [], None, []
    def flush():
        if title and buf:
            notes.append(f"[impl-summary:{title}] " + " ".join(buf).strip())
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            flush()
            title, buf = line[3:].strip(), []
        elif title:
            buf.append(line.strip())
    flush()
    return notes


def _mcp_tool_notes(pool, settings: Settings) -> list[str]:
    """Capture every @mcp.tool() function's name + signature + docstring."""
    from .mcp_server import (tools_contracts, tools_issues, tools_memory,
                             tools_skills, tools_status)

    class _Rec:
        def __init__(self): self.fns = []
        def tool(self):
            def deco(fn): self.fns.append(fn); return fn
            return deco

    modules = [
        ("issues", lambda r: tools_issues.register(r, pool)),
        ("memory", lambda r: tools_memory.register(r, pool, settings)),
        ("skills", lambda r: tools_skills.register(r, pool)),
        ("status", lambda r: tools_status.register(r, pool, settings)),
        ("contracts", lambda r: tools_contracts.register(r, pool)),
    ]
    notes = []
    for name, reg in modules:
        rec = _Rec()
        try:
            reg(rec)
        except Exception:  # noqa: BLE001 - skip a module that won't introspect
            continue
        lines = []
        for fn in rec.fns:
            doc = (inspect.getdoc(fn) or "").split("\n")[0]
            lines.append(f"{fn.__name__}{inspect.signature(fn)} — {doc}")
        if lines:
            notes.append(f"[mcp-tools:{name}] " + " | ".join(lines))
    return notes


def _repository_api_note() -> str:
    members = inspect.getmembers(repo, inspect.isfunction)
    lines = []
    for fname, fn in members:
        if fname.startswith("_") or fn.__module__ != repo.__name__:
            continue
        doc = (inspect.getdoc(fn) or "").split("\n")[0]
        lines.append(f"{fname}{inspect.signature(fn)}" + (f" — {doc}" if doc else ""))
    return ("[repository-api] ALL DB writes go through orchestrator.repository. "
            "Public functions: " + " | ".join(sorted(lines)))


def _config_notes() -> list[str]:
    notes = []
    for fname in ("settings.yaml", "pipelines.yaml", "roster.yaml"):
        p = CONFIG_DIR / fname
        if p.exists():
            notes.append(f"[config:{fname}]\n{p.read_text()}")
    env = REPO_ROOT / ".env.example"
    if env.exists():
        notes.append(f"[config:.env.example]\n{env.read_text()}")
    return notes


def collect_notes(pool, settings: Settings) -> list[str]:
    """Gather all KB note bodies from the tool's authoritative sources."""
    notes = [_CONTRACT_SPEC_NOTE]
    notes += _migration_notes()
    notes += _impl_summary_notes()
    notes += _mcp_tool_notes(pool, settings)
    notes.append(_repository_api_note())
    notes += _config_notes()
    return [n for n in notes if n and n.strip()]


def build_monitor_kb(pool, settings: Settings) -> int:
    """Wipe and rebuild the monitor:kb scope from current source. Returns count."""
    embedder = make_embedder(settings)
    repo.memory_clear_scope(pool, MONITOR_KB_SCOPE)
    n = 0
    for body in collect_notes(pool, settings):
        embedding = embedder.embed(body) if embedder is not None else None
        repo.memory_write(pool, body, scope=MONITOR_KB_SCOPE, embedding=embedding)
        n += 1
    return n


def monitor_kb_empty(pool) -> bool:
    return not repo.memory_recall(pool, scope=MONITOR_KB_SCOPE, limit=1)


def bootstrap_monitor_kb(pool, settings: Settings) -> int:
    """Build the KB only if the scope is empty (idempotent install hook)."""
    if monitor_kb_empty(pool):
        return build_monitor_kb(pool, settings)
    return 0
