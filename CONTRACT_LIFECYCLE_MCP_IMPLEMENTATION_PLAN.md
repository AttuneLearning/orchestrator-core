# CadenceLMS Contract Lifecycle MCP Implementation Plan

Status: Proposed implementation plan  
Owner: orchestration  
Primary use case: administrator-approved notification contract reconciliation  
Related issue: #98  
Related contracts: #3, #5, #6, #7, #8, #9, #10, #11

## Objective

Expose a privileged, auditable MCP operation for contract lifecycle changes. An
authorized administrator must be able to preview and atomically apply contract
transitions without direct database writes, duplicate proposals, or untracked
dashboard mutations.

The first controlled batch is:

- supersede or retire #3 `GET /notifications`, replacement #6
- supersede or retire #5 `POST /notifications/read-all`, replacement #7
- promote #6–#9 to `agreed`
- preserve #10 and #11 as `agreed`
- record source, DTO/type, replacement, authorization, reason, and audit data

## Current gap

The exposed contract MCP surface currently supports lookup, listing, issue-scoped
lookup, and proposal. It does not support status transitions,
replacement/supersession metadata, administrator authorization, bulk atomic
reconciliation, lifecycle audit events, dry-run previews, idempotent retries,
optimistic concurrency, or route consistency validation.

## MCP operations

### `contract_lifecycle_preview`

Inputs:

- project
- operation ID
- authenticated actor context
- reason
- proposed changes
- optional expected contract versions/content hashes

Outputs:

- validity
- normalized changes
- conflicts and warnings
- affected contracts
- expected audit event
- immutable preview token or validation digest

Preview must not mutate state.

### `contract_lifecycle_apply`

Inputs:

- project
- operation ID
- authenticated administrator actor context
- reason
- proposed changes or validated preview token
- expected versions/content hashes

Outputs:

- `applied`, `rejected`, or `conflict`
- operation ID
- changed and unchanged contract IDs
- audit event ID
- warnings
- resulting contract summaries

The operation must be idempotent. Repeating an operation ID returns the original
result and does not duplicate lifecycle events.

### `contract_lifecycle_history`

Return append-only lifecycle events by contract ID or operation ID, including
actor, reason, before/after state, replacement references, and source metadata.

## Lifecycle model

Confirm and document the supported states and transitions. Recommended states:

- `proposed`
- `agreed`
- `deprecated`
- `superseded`
- `retired`
- `rejected`

Recommended transitions:

- `proposed` -> `agreed` or `rejected`
- `agreed` -> `deprecated` or `superseded`
- `deprecated` -> `superseded` or `retired`
- `superseded` -> `retired`

A superseded contract must identify its replacement. A retired contract must
not be resolvable as an active dependency. If the existing schema cannot add a
`superseded` state, use `retired` with `superseded_by_contract_id` while
preserving the distinction in lifecycle metadata.

## Authorization and transaction rules

The service must verify administrator authorization at the orchestrator
boundary; a caller-supplied administrator boolean is insufficient. Record actor
identity, authenticated role evidence, project, operation ID, reason, timestamp,
affected IDs, source client/session, and result.

Apply must use one transaction for precondition reads, lifecycle changes,
replacement metadata, audit-event insertion, and idempotency registration. Use
optimistic concurrency with a version, update timestamp, or content hash. If any
target changed after preview, return a conflict and apply nothing.

Never allow partial success, arbitrary SQL, silent downgrade of an agreed
contract, or an un-attributed mutation.

## Validation rules

Before applying a batch, validate:

- every ID exists and belongs to the requested project
- every action is an allowed transition
- no issue IDs are accepted in place of contract IDs
- no duplicate IDs occur in the request
- replacement contracts exist, are not retired, and are not self-replacements
- only one active contract exists for each normalized method/path pair
- no active top-level `/notifications` route competes with the approved
  `/users/me` notification route family
- canonical routes have request, response DTO, shared type, source, auth, and
  version/content-hash references
- the registry agrees with API routes, shared DTOs/types, frontend endpoint
  registries/mocks, and `contracts.audit.json`

Differences must be returned as blocking conflicts or explicit administrator-
accepted warnings.

## Data and audit model

Persist lifecycle metadata directly or in an append-only lifecycle table:

- contract ID
- previous and resulting status
- lifecycle action and reason
- replacement contract ID
- actor ID and role
- operation ID
- source/request/response/type/auth references
- created/effective timestamps
- content hash or version

Add a project-scoped unique constraint for operation IDs. Emit one queryable
batch event containing all changes, or one batch event plus per-contract events.
Each event must include project, operation ID, actor, reason, timestamp,
requested changes, before/after state, replacements, warnings, validation result,
and audit source.

## Initial notification reconciliation

Apply the following only after validation succeeds:

| Contract | Route | Action |
|---:|---|---|
| #3 | `GET /notifications` | supersede/retire; replacement #6 |
| #5 | `POST /notifications/read-all` | supersede/retire; replacement #7 |
| #6 | `GET /users/me/notifications` | agree |
| #7 | `POST /users/me/notifications/mark-all-read` | agree |
| #8 | `GET /users/me/notifications/unread-count` | agree |
| #9 | `PATCH /users/me/notifications/:notificationId/read` | agree |
| #10 | `GET /users/me/notification-preferences` | preserve agreed |
| #11 | `PATCH /users/me/notification-preferences` | preserve agreed |

Known warnings to resolve or explicitly accept with audit evidence:

- normalize `bearer` versus `jwt` auth metadata against the actual route policy
- add missing type references for canonical routes
- add explicit request/response references for #10 and #11
- reconcile #9 with the current implementation if it still exposes
  `PATCH /users/me/notifications/:id`
- use API route declaration source references, not only planning-document refs

## Dashboard integration

The Contracts page should provide Preview, Apply approved batch, and lifecycle
history actions. It should display superseded/retired status distinctly,
replacement links, actor, reason, audit event, conflicts, warnings, and source
metadata. The dashboard must not display a transition as complete until MCP
returns an applied result.

## Verification

Test authorization, valid and invalid transitions, atomic rollback, missing or
self-replacements, duplicate routes, stale-version conflicts, idempotent retry,
exactly-once audit events, history read-back, prevention of retired active
dependencies, preview non-mutation, and dashboard rendering.

Notification acceptance checks:

- #3 is inactive and points to #6
- #5 is inactive and points to #7
- #6–#9 are `agreed`
- #10–#11 remain `agreed`
- no active duplicate notification routes exist
- API, frontend, registry, and audit snapshot agree
- lifecycle history returns the administrator operation and all changes

## Delivery sequence

1. Confirm status vocabulary and transition policy.
2. Add lifecycle metadata and append-only event persistence.
3. Implement authorization and project scoping.
4. Implement preview validation.
5. Implement transactional apply with idempotency and optimistic concurrency.
6. Implement lifecycle history.
7. Add MCP schemas and structured responses.
8. Add dashboard status, replacement, and audit rendering.
9. Add route/DTO/audit consistency verification.
10. Apply the notification batch as the first controlled reconciliation.
11. Re-read all affected contracts and record verification evidence.
12. Add recurring registry drift detection.

## Definition of done

An administrator can preview and apply a contract lifecycle batch through MCP;
the operation is authorized, transactional, idempotent, and auditable;
replacement metadata is visible; canonical-route checks run during apply; the
notification batch can be applied without direct database access; and registry,
dashboard, audit history, API routes, and DTO/type snapshots agree.
