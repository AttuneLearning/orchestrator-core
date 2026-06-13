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
import re
from pathlib import Path
from typing import Optional

from . import repository as repo
from .config import CONFIG_DIR, MIGRATIONS_DIR, REPO_ROOT, Settings
from .embeddings import make_embedder

MONITOR_KB_SCOPE = "monitor:kb"

# Authored ground truth for the question that exposed the gap: the contract-store
# ingestion format the `import-contracts` CLI / repository.upsert_contract expect.
_CONTRACT_SPEC_NOTE = (
    "[contract-store] Seed format for `import-contracts <file>` / repository.upsert_contract: "
    "a FLAT JSON ARRAY (no envelope, no _meta), one object per endpoint, idempotent UPSERT "
    "keyed on (method, path). REQUIRED fields: method, path. OPTIONAL fields (with defaults): "
    "request_ref='' (zod validator export name), response_dto='' (DTO type name), auth='none', "
    "owner_team='backend', status='proposed', version='1.0', source_ref=null. status lifecycle "
    "is one of: proposed | agreed | live | deprecated (seed data uses 'live' if the route is "
    "wired, else 'proposed'; a contract is 'satisfied' when status is agreed OR live). Validators "
    "and DTOs are referenced BY NAME, never inlined. content_hash is computed by the importer as "
    "sha256 of method|path|request_ref|response_dto ONLY (NOT the whole record — auth/owner_team/"
    "status/version do not change the hash). The pull-fe contract_check gate blocks frontend "
    "new-endpoint issues until the endpoints they consume are satisfied (see migration 0011)."
)


def _migration_notes() -> list[str]:
    notes = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text()
        # The leading `-- ...` comment block is the human-written intent.
        header = [ln[2:].strip() for ln in text.splitlines()
                  if ln.strip().startswith("--") and "===" not in ln]
        desc = " ".join(h for h in header if h)
        # Include the actual DDL (column defs with NOT NULL/DEFAULT clauses + inline
        # comments) — that's the ground truth for required-vs-optional and enums.
        ddl = " ".join(ln.strip() for ln in text.splitlines()
                       if ln.strip() and not ln.strip().startswith("--"))
        notes.append(f"[schema:{path.name}] {desc} || DDL: {ddl}")
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


def _signature_notes() -> list[str]:
    """One note per public repository function — its EXACT signature (defaults make
    required-vs-optional explicit) + first docstring line. Individually retrievable
    so the relevant function (e.g. upsert_contract) surfaces for a specific question.
    ALL DB writes go through these."""
    notes = []
    for fname, fn in inspect.getmembers(repo, inspect.isfunction):
        if fname.startswith("_") or fn.__module__ != repo.__name__:
            continue
        doc = (inspect.getdoc(fn) or "").split("\n")[0]
        notes.append(f"[repo:{fname}] {fname}{inspect.signature(fn)}"
                     + (f" — {doc}" if doc else ""))
    return notes


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
    notes += _signature_notes()
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


def retrieve_context(pool, query: str, limit: int = 8) -> list[str]:
    """Retrieve grounding notes for a question via keyword-overlap ranking over the
    monitor KB. Deterministic and robust for a small curated KB — the stub embedder's
    vector ranking is unreliable at this size, while term overlap reliably surfaces
    the right notes (e.g. a contract question pulls the [contract-store] note, the
    0011 DDL, and the upsert_contract signature). Returns note bodies, best first."""
    terms = set(re.findall(r"[a-z][a-z0-9_]{3,}", query.lower()))
    if not terms:
        return []
    scored = []
    for n in repo.memory_recall(pool, scope=MONITOR_KB_SCOPE, limit=1000):
        body_l = n.body.lower()
        score = sum(1 for t in terms if t in body_l)
        if score:
            scored.append((score, n.id, n.body))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [b for _, _, b in scored[:limit]]


def monitor_kb_empty(pool) -> bool:
    return not repo.memory_recall(pool, scope=MONITOR_KB_SCOPE, limit=1)


def bootstrap_monitor_kb(pool, settings: Settings) -> int:
    """Build the KB only if the scope is empty (idempotent install hook)."""
    if monitor_kb_empty(pool):
        return build_monitor_kb(pool, settings)
    return 0
