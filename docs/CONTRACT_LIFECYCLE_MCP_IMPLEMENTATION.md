# Contract Lifecycle MCP — Engineering Implementation Plan

Derived from `CONTRACT_LIFECYCLE_MCP_IMPLEMENTATION_PLAN.md` (the requirements
spec). This document maps that spec onto the actual orchestrator codebase:
concrete files, function signatures, DDL, MCP tool shapes, and tests. Read the
spec for *what* and *why*; read this for *where* and *how*.

Status: proposed
Next migration number: **0023** (0022 = `pending_actions` already exists; CLAUDE.md's
"next 0022" note is stale)

---

## 0. Review of the spec against this codebase

The spec is sound and mostly aligns with existing patterns. Key reconciliations
and one genuine design tension to resolve before coding:

| Spec requirement | Existing reality | Decision |
|---|---|---|
| Privileged, boundary-verified authorization; caller boolean insufficient | `ORCH_ROLE` env → `actor_role` captured in `build_server()`, `_require_orch_manager()` in `tools_issues.py:64`. Roles are never tool args. | **Reuse this exact mechanism.** Administrator = `orch-manager` role. No new auth infra. |
| MCP surface for contract mutation | GAP-2 (2026-07-12) deliberately **removed** contract mutation from the worker MCP surface; `contract_agree`/`contract_upsert` are human/dashboard-gated. Workers may only `contract_propose` (rate-limited). | **Tension.** New lifecycle tools are admin-only and must be gated to `orch-manager`, never the general worker surface. Register them so a worker session (any other role) is refused. Document this as an intentional, narrow re-opening for the coordinator only. |
| Lifecycle states `superseded`, `retired`, `rejected` | `contracts.status` today: `proposed \| agreed \| live \| deprecated`. `_SATISFIED = ("agreed","live")`. | Add the three states as allowed values (TEXT column, no enum — additive). Update no CHECK constraint exists to change. |
| Retired contract not resolvable as active dependency | `contract_satisfied()` = `status IN _SATISFIED`; the engine's unblock gates on it. | Retired/superseded/deprecated/rejected are all **outside** `_SATISFIED` — already excluded. Verify no code treats `deprecated` as active; add regression test. |
| Superseded contract identifies replacement | No replacement column. | Add `superseded_by_contract_id BIGINT REFERENCES contracts(id)` to `contracts`. |
| Optimistic concurrency (version / hash / update ts) | `contracts` has `version`, `content_hash`, `updated_at`. | Use `updated_at` timestamp as the concurrency token (simplest, already maintained). Optionally accept `content_hash` too. Preview returns tokens; apply re-reads under lock and compares. |
| Append-only lifecycle audit events | `issue_events` is the only audit log and is **issue-scoped** (FK to issues). Contract lifecycle is not issue-scoped. | New append-only table `contract_lifecycle_events` (migration 0023). Mirrors `issue_events` philosophy (invariant #1: writes via repository.py only). |
| Idempotent apply by operation ID | No idempotency store. | Project-scoped unique constraint on operation ID inside `contract_lifecycle_events` (batch row), OR a small `contract_lifecycle_ops` table keyed `(project, operation_id)`. Recommend the ops table (clean replay of the original result). |
| Project scoping | `contracts` has **no `project` column** — this orchestrator DB is single-project per deployment. | Treat `project` as a passthrough/validation field recorded in audit, defaulting to the deployment's configured project. Do **not** add a project column to `contracts` in this batch unless multi-project is a real requirement — flag as an open decision. |
| Dashboard preview/apply/history | `/contracts` page exists (`app.py:1065`, `templates.py:795`) with accept/reject/removal actions driven by `contracts_overview()`. | Extend the same page + `templates.py`; reuse the `repository_confirmation` typed-confirmation idiom already used for bulk rebuild. |

**Recommendation on the tension:** implement the MCP tools (spec is explicit it
wants them) but gate strictly to `orch-manager`, and make the dashboard call the
*same* repository functions the MCP tools call — one write path, two front doors.

---

## 1. Data model (migration `0023_contract_lifecycle.sql`)

Additive only. No data migration needed.

```sql
-- 0023_contract_lifecycle — lifecycle states, replacement metadata, and an
-- append-only lifecycle audit/idempotency layer for admin-driven contract
-- reconciliation. All writes go through repository.py (invariant #1).

-- Replacement pointer for supersede/retire.
ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS superseded_by_contract_id BIGINT
        REFERENCES contracts(id);

-- Idempotency + batch identity. One row per apply operation.
CREATE TABLE IF NOT EXISTS contract_lifecycle_ops (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project       TEXT        NOT NULL DEFAULT '',
    operation_id  TEXT        NOT NULL,
    actor         TEXT        NOT NULL,
    actor_role    TEXT        NOT NULL,
    reason        TEXT        NOT NULL DEFAULT '',
    source        TEXT        NOT NULL DEFAULT '',   -- client/session metadata
    result        TEXT        NOT NULL,              -- applied | rejected | conflict
    requested      JSONB      NOT NULL,              -- the normalized change batch
    response       JSONB      NOT NULL,              -- the full apply result (for replay)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project, operation_id)
);

-- Append-only per-contract lifecycle events (one batch = N rows, all sharing op).
CREATE TABLE IF NOT EXISTS contract_lifecycle_events (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    op_id         BIGINT      NOT NULL REFERENCES contract_lifecycle_ops(id),
    contract_id   BIGINT      NOT NULL REFERENCES contracts(id),
    method        TEXT        NOT NULL,
    path          TEXT        NOT NULL,
    action        TEXT        NOT NULL,              -- agree|supersede|retire|deprecate|reject
    from_status   TEXT,
    to_status     TEXT        NOT NULL,
    superseded_by_contract_id BIGINT,
    reason        TEXT        NOT NULL DEFAULT '',
    actor         TEXT        NOT NULL,
    source_ref    TEXT,
    content_hash  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_contract
    ON contract_lifecycle_events(contract_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_op
    ON contract_lifecycle_events(op_id);
```

Add `superseded`, `retired`, `rejected` as recognized `contracts.status` values
in code (`repository.py`). `_SATISFIED` stays `("agreed","live")` — every new
state is intentionally non-satisfying.

---

## 2. Lifecycle model (pure, no I/O)

Add a small transition table. Candidate home: a new pure module
`orchestrator/contract_lifecycle.py` (mirrors the purity of `state_machine.py`,
invariant #2) — no DB imports.

```python
# orchestrator/contract_lifecycle.py
ALLOWED = {
    "proposed":   {"agreed", "rejected"},
    "agreed":     {"deprecated", "superseded", "retired"},
    "live":       {"deprecated", "superseded", "retired"},
    "deprecated": {"superseded", "retired"},
    "superseded": {"retired"},
    "retired":    set(),
    "rejected":   set(),
}
ACTION_TO_STATUS = {
    "agree": "agreed", "reject": "rejected", "deprecate": "deprecated",
    "supersede": "superseded", "retire": "retired",
}
REQUIRES_REPLACEMENT = {"supersede"}   # retire may optionally carry one

def can_transition(frm: str, to: str) -> bool: ...
def validate_batch(changes, contracts_by_id) -> (normalized, conflicts, warnings): ...
```

`validate_batch` is where every spec validation rule lives (pure, unit-testable):
IDs exist & (in-project), allowed transition, no issue-IDs-as-contract-IDs, no
dup IDs, replacement exists / not retired / not self, single active contract per
normalized `(method, path)`, no active top-level `/notifications` vs `/users/me`
family collision, canonical-route field completeness. It returns blocking
`conflicts` vs advisory `warnings`.

Route normalization: reuse the existing `(method, path)` keying; normalize path
params (`:id` vs `{id}`) with one helper so `/x/:id` and `/x/{id}` collide.

---

## 3. Repository layer (`orchestrator/repository.py`)

All new writes here (invariant #1). New functions:

- `contract_lifecycle_preview(pool, project, operation_id, actor, actor_role, reason, changes, expected=None) -> dict`
  Read-only. Loads affected contracts, calls `contract_lifecycle.validate_batch`,
  computes each target's current concurrency token (`updated_at`/`content_hash`),
  and returns `{valid, normalized_changes, conflicts, warnings, affected,
  expected_event, preview_token}`. **No writes.** `preview_token` = digest over
  `(operation_id, normalized_changes, tokens)`.

- `contract_lifecycle_apply(pool, project, operation_id, actor, actor_role, reason, changes, expected=None, source="", confirm_project=None) -> dict`
  Single `with pool.connection() as conn, conn.transaction():` block:
  1. `SELECT ... FOR UPDATE` the target contract rows.
  2. Idempotency: if `contract_lifecycle_ops` already has `(project, operation_id)`,
     return its stored `response` verbatim (no new events).
  3. If the batch has any destructive action (§11) and `confirm_project` does not
     match the configured project name → record a `rejected` op and return
     `reason='confirmation_required'`, applying nothing.
  4. Re-validate under lock; re-check concurrency tokens vs `expected`. Any
     mismatch → insert an ops row with `result='conflict'` and return conflict,
     **apply nothing** (transaction still commits only the conflict record).
  4. Apply each change: `UPDATE contracts SET status=..., superseded_by_contract_id=...,
     updated_at=now()`; insert one `contract_lifecycle_events` row per change.
  5. Insert the `contract_lifecycle_ops` row (`result='applied'`, full `response`).
  Return `{result, operation_id, changed, unchanged, audit_op_id, warnings, summaries}`.

- `contract_lifecycle_history(pool, contract_id=None, operation_id=None) -> list[dict]`
  Read from `contract_lifecycle_events` (+ join ops for actor/reason/source).

Reuse `set_contract_status` internals but extend to carry
`superseded_by_contract_id`; keep `_contract_hash` for tokens. Extend
`_CONTRACT_COLS` to include `superseded_by_contract_id`.

Concurrency choice: `updated_at` is the primary token (already bumped on every
write). Accept optional `content_hash` in `expected` for callers that prefer it.

---

## 4. MCP tools (`orchestrator/mcp_server/tools_contracts.py`)

`register(mcp, pool)` must also receive `actor_role` (thread it through
`server.py`'s `tools_contracts.register(mcp, pool, actor_role=server_role)`).
Add a module-level `_require_admin(actor_role)` identical in spirit to
`_require_orch_manager` (accept `orch-manager`).

Three new `@mcp.tool()`s, each calling `_require_admin(actor_role)` first:

- `contract_lifecycle_preview(project, operation_id, reason, changes, expected=None)`
- `contract_lifecycle_apply(project, operation_id, reason, changes, expected=None, preview_token=None, confirm_project=None)` — `confirm_project` required for destructive batches (§11)
- `contract_lifecycle_history(contract_id=None, operation_id=None)`

`changes` shape (list of objects): `{contract_id, action, replacement_contract_id?,
source_ref?, request_ref?, response_dto?, type_ref?, auth?}`. Actor identity is
taken from the trusted session (env `ORCH_ACTOR`/`ORCH_ROLE`), **never** a tool arg
(same rule the spec demands and the codebase already enforces).

Structured responses: return plain dicts (FastMCP serializes). Mirror the
existing tools' concise dict style.

---

## 5. Dashboard (`orchestrator/dashboard/app.py`, `templates.py`)

`/contracts` already renders `contracts_overview()`. Add:

- `GET /contracts/lifecycle` (or extend the existing page) showing status badges
  for `superseded`/`retired`/`rejected` distinctly, replacement links (`superseded_by`),
  and a "Lifecycle history" expander per contract from `contract_lifecycle_history`.
- `POST /contracts/lifecycle/preview` → renders the preview (conflicts/warnings/affected).
- `POST /contracts/lifecycle/apply` → calls `repo.contract_lifecycle_apply(...)`
  with `actor="dashboard-admin"`, `actor_role="orch-manager"`. Reuse the
  `repository_confirmation` typed-confirmation idiom (`app.py:1099`) for the batch apply.
  The UI must not show a transition as done until the repo returns `result='applied'`.

`contracts_overview()` / `_contract_state()` (`repository.py:2137`) gain the new
states so cards render them. `templates.py:_contract_fields` shows
`superseded_by` as a link.

---

## 6. First controlled batch (the #98 notification reconciliation)

Driven entirely through the new apply path — no direct SQL. Two options:

- **Preferred:** a one-shot script/CLI (`python -m orchestrator.cli contracts-lifecycle-apply
  --op notif-reconcile-2026-07 --file batch.json`) that calls
  `repo.contract_lifecycle_apply`, so it's auditable and idempotent.
- Or the dashboard "Apply approved batch" button.

Batch = the spec's table (#3→supersede/#6, #5→supersede/#7, #6–#9 agree, #10/#11
preserve). Resolve the listed warnings (bearer-vs-jwt auth, missing type_refs,
#10/#11 request/response refs, #9 route reconciliation) as either fixes recorded
in `source_ref`/`type_ref` or explicit administrator-accepted warnings in the audit.

---

## 7. Consistency verification (`verify_report.py` / new checker)

Extend the existing contract-audit tooling (`refresh_contract_audit`,
`load_contract_audit`, `bulk_rebuild_contracts` at `repository.py:1976+`) with a
read-only drift checker that compares the registry against API routes, shared
DTOs/types, frontend endpoint registries/mocks, and `contracts.audit.json`, and
surfaces differences as blocking conflicts (apply-time) or a recurring dashboard
report (step 12). This is the largest external-facing piece and can ship after
the core lifecycle engine.

---

## 8. Test plan (`tests/test_contract_lifecycle.py`, + dashboard test)

Pure (`contract_lifecycle.py`) — no DB:
- allowed/disallowed transition matrix; replacement-required; self-replacement;
  retired-replacement; dup IDs; issue-ID-as-contract-ID; route-collision; canonical
  field completeness; warning vs conflict classification.

Repository (uses `pool` fixture, `tests/conftest.py`):
- preview does not mutate (row `updated_at` unchanged after preview);
- apply is atomic (inject a mid-batch failure → nothing applied);
- stale-token conflict → applies nothing, records a `conflict` op;
- idempotent retry (same `operation_id` twice → identical response, exactly one
  set of events, no duplicate ops row);
- history read-back by contract_id and operation_id;
- retired/superseded contract is **not** `contract_satisfied` (regression:
  engine unblock won't treat it as active);
- authorization: MCP tool with non-`orch-manager` role raises `PermissionError`
  (mirror the `_require_orch_manager` tests);
- destructive batch (supersede/retire) with wrong/missing `confirm_project` →
  `rejected` (`confirmation_required`), nothing applied; correct project name →
  applies; pure-`agree` batch applies with no `confirm_project`.

Dashboard (`TestClient`, mirror `test_contract_acceptance.py`):
- preview renders conflicts/warnings; apply shows applied only on repo success;
  superseded/retired render distinctly with replacement links.

Acceptance (the #98 checks): #3 inactive→#6, #5 inactive→#7, #6–#9 agreed,
#10–#11 agreed, no active duplicate notification routes, history returns the op.

Run: `service postgresql start && .venv/bin/python -m orchestrator.cli migrate &&
.venv/bin/python -m pytest -q` — expect green (one suite at a time; truncation races).

---

## 9. Delivery sequence (maps to spec §Delivery)

1. **Migration 0023** + extend `_CONTRACT_COLS`, add new states in code. (§1)
2. **`contract_lifecycle.py`** pure module + unit tests. (§2)
3. **Repository** preview/apply/history + auth-free core. (§3)
4. **MCP tools** + `_require_admin`, thread `actor_role` through `server.py`. (§4)
5. **Dashboard** preview/apply/history + rendering. (§5)
6. **CLI one-shot** for batch apply. (§6)
7. **Notification batch** applied via apply path; record evidence. (§6)
8. **Consistency checker** + recurring drift report. (§7)

Steps 1–4 are the core and can land as one reviewable unit; 5–8 layer on top.

---

## 10. Resolved decisions (locked 2026-07-20)

1. **Project scoping** — `project` is an **audited passthrough**, validated
   against the deployment's configured project. No `project` column added to
   `contracts` in this batch. Multi-project contracts deferred.
2. **`superseded`** is a **first-class state** (plus `superseded_by_contract_id`
   column). Not folded into `retired`.
3. **Administrator role** = the existing **`orch-manager`** session role. No new
   `admin` role.
4. **Idempotency** via the dedicated **`contract_lifecycle_ops`** table (clean
   replay of the original response).
5. **Destructive-action confirmation** (new) — `supersede` and `retire` (the
   actions that pull a contract out of the active set) require typing the project
   name to confirm. See §11.

## 11. Destructive-action confirmation

Actions whose `to_status` leaves `_SATISFIED` when the contract was previously
satisfied — i.e. `supersede`, `retire`, and `deprecate` of an `agreed`/`live`
contract — are "destructive" and require an explicit project-name confirmation:

- **MCP** `contract_lifecycle_apply` gains a required `confirm_project` arg. If
  the batch contains any destructive action, the apply is refused (returns
  `result='rejected'`, reason `confirmation_required`) unless
  `confirm_project == <configured project name>`. Non-destructive batches (pure
  `agree`/`reject`) do not require it. `contract_lifecycle_preview` reports which
  changes are destructive and whether confirmation will be required, so the
  caller knows before applying.
- **Dashboard** reuses the existing `repository_confirmation` typed-input idiom
  (`app.py:1099`, "type the repo/project name exactly to enable the button"):
  the "Apply approved batch" button stays disabled until the project name is
  typed, whenever the batch includes a destructive action.

The confirmation is recorded in the `contract_lifecycle_ops.response` audit
payload (confirmed: true, value matched) so the audit shows the administrator
consciously confirmed the destructive batch.
