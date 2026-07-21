# CadenceLMS — Project-Wide Contract Optimization Plan

> **Status:** Coordinator recommendation
>
> **Date:** 2026-07-20
>
> **Scope:** CadenceLMS backend, frontend, contracts package, tests, mocks,
> CI, pseudo-production verification, and orchestrator issue/gate workflow.
>
> **Purpose:** Establish a single, durable contract system before delegating
> further cross-stack feature implementation to smaller workers.

## Recommendation

Make project-wide contract optimization the first enabling project for the
next CadenceLMS implementation cycle.

The immediate priority should not be adding more notification or workspace
feature code. It should be deciding, publishing, validating, and enforcing the
contracts that connect the backend, frontend, test mocks, generated artifacts,
CI, and pseudo-production verification.

This is an enabling project, not a product feature. Its output is a stable
contract surface on which the notification, workspace-selection, and future LMS
features can be implemented by GPT-5.4-mini or other workers with much lower
ambiguity and regression risk.

The proposed order is:

```text
inventory → decide → canonicalize → generate → verify → enforce → migrate
```

The orchestrator should own the decision records, issue dependencies, gate
policy, and promotion. Workers may implement bounded contract changes after the
decision is recorded; they should not independently choose competing API
shapes.

## Why this should come first

CadenceLMS currently has contract drift at several boundaries:

- notification routes differ between proposed contracts and live routes;
- the individual mark-read shape is not sufficiently reconciled;
- `/auth/me` and `/auth/context` are proposed but not yet finalized;
- contract export and web verification rely on failed or legacy paths;
- CI verification is blocked by the unstable root contract command;
- frontend MSW handlers and endpoint registries can become green while the
  production API remains different;
- long verification runs are being treated as failures because the harness
  timeout is shorter than the actual suite duration.

When these boundaries are unresolved, implementation produces false progress:
the backend can compile, the frontend can pass mocked tests, and a worker can
report a green SHA while the integrated product still disagrees about routes,
DTOs, status codes, or generated artifacts.

Contract optimization gives the project one shared vocabulary and one evidence
path. It also makes task decomposition practical: a small worker can implement
an endpoint, handler, or test when the interface is already settled.

## What “contract” includes

For this project, a contract is more than an OpenAPI-like route definition. The
contract system should cover:

| Boundary | Contract contents |
| --- | --- |
| HTTP API | Method, path, auth requirements, request, response, status codes, errors, pagination, idempotency |
| Domain events | Event name, version, producer, payload, ordering, delivery semantics, retry behavior |
| Persistence | Entity fields, nullability, ownership, lifecycle, uniqueness, read/delivery state |
| Session/auth | Current-user shape, workspace membership, context switching, 401/403 behavior, persistence |
| Frontend data access | Endpoint registry, DTO usage, cache/invalidation rules, loading/error states |
| Test doubles | MSW routes, fixtures, seed personas, captured email behavior |
| Generated artifacts | Source location, generation command, output location, freshness and parseability |
| Verification | Required commands, timeout class, JUnit report location, failure classification |
| Operational handoff | Issue acceptance criteria, evidence format, gate owner, promotion condition |

The goal is not to freeze every internal implementation detail. The goal is to
make every cross-boundary assumption explicit and machine-checkable.

## Contract questions that must be answered

### 1. Where is the canonical source?

Questions:

- Which checked-in package or directory is the authoritative source for API
  contracts?
- Are generated JSON and TypeScript declarations committed, generated in CI,
  or both?
- Is the old `dev_communication/shared/contracts/dist` location retired,
  mirrored temporarily, or forbidden?
- What command generates artifacts from a clean checkout?
- How does CI determine that generated artifacts are stale?

Recommendation:

Choose one in-repository source package. Generated artifacts should have one
documented output location and one generation command. Compatibility copies or
symlinks should not become a second source of truth.

### 2. What is the API naming and versioning policy?

Questions:

- Should user-scoped resources use `/users/me/...` or top-level routes such as
  `/notifications`?
- Do route changes require a versioned endpoint, a migration window, or a
  coordinated atomic change?
- How are deprecated routes marked and removed?
- Are IDs opaque strings, UUIDs, numeric values, or mixed by resource?
- What casing and naming conventions apply to JSON fields?

Recommendation:

Use one consistent resource policy. For authenticated user-owned resources,
prefer the established project convention and document it once. Do not add a
second route merely because a proposed contract uses a different prefix.

### 3. What does each status code mean?

Questions:

- When does an endpoint return 400 versus 401 versus 403 versus 404?
- Is an unauthorized workspace selection always 403, as required by the
  workspace issue?
- Are validation errors structurally consistent across endpoints?
- Do empty collections return 200 with an empty list, or another status?
- How are conflict and duplicate operations represented?

Recommendation:

Define a shared error envelope and status-code matrix. Security-sensitive
authorization failures must be tested explicitly, not inferred from frontend
behavior.

### 4. What are the notification contracts?

Questions:

- Is the canonical feed route `GET /users/me/notifications` or
  `GET /notifications`?
- What are the individual read and read-all routes?
- Does mark-read use `PATCH`, `POST`, or another method?
- Is read state represented by `readAt`, `isRead`, or both?
- How are unread counts returned?
- What pagination model is used: cursor, offset, or page number?
- What fields link a notification to a course, enrollment, assignment, or
  other LMS object?
- Which delivery states are distinct: pending, delivered, failed, skipped,
  or read?
- Are notification preferences category-level, channel-level, or both?
- What makes a producer operation idempotent?

Recommendation:

Finalize one notification DTO and route family before implementing the feed,
MSW handlers, badge, or preferences. Keep product read state separate from
transport delivery state. Define an idempotency key for event-to-notification
creation so retries cannot create duplicate user messages.

### 5. What are the event contracts?

Questions:

- Which events are public domain events versus internal implementation events?
- Does an event publish only after the transaction commits?
- Is delivery at-least-once, at-most-once, or exactly-once from the consumer’s
  perspective?
- How are event versions evolved?
- What is the retry and dead-letter behavior?
- Is event ordering required per user, course, or aggregate?

Recommendation:

Start with a documented versioned event envelope and at-least-once delivery
with idempotent consumers. Do not promise exactly-once behavior unless the
storage and queue implementation can prove it.

### 6. What is the workspace/session contract?

Questions:

- What does `/auth/me` return for one versus multiple workspace memberships?
- Does it return all active memberships, only selectable memberships, or a
  separate workspace collection?
- How is the current workspace represented?
- Does `PATCH /auth/context` return the updated current-user DTO, a session
  token, or a minimal acknowledgement?
- Where is selected context persisted: server session, token claims, cookie,
  or another existing mechanism?
- What happens when a membership is removed while a user has an active session?
- Is department membership sufficient authorization, or are role and course
  scopes also required?

Recommendation:

Keep one authentication/session mechanism. Validate every requested context
against active memberships on the server, return 403 for an unauthorized
department, and make the next `/auth/me` response the authoritative view of
the selected context.

### 7. How do frontend and test doubles stay aligned?

Questions:

- Are frontend endpoint constants generated, manually maintained, or checked
  against the contract registry?
- Must MSW handlers use the same DTO types as production clients?
- Which fixtures represent single-workspace, multi-workspace, unread, and
  failed-delivery users?
- Do mock handlers model the same status codes and error envelopes?
- How do we prevent a mock-only route from surviving after a backend route is
  changed?

Recommendation:

Make endpoint registry entries and MSW handlers consume generated or shared
contract types where practical. Add a contract verification test that compares
mocked routes and required production routes for the supported frontend flows.

### 8. What evidence makes a contract change complete?

Questions:

- Which unit, integration, typecheck, contract, and UAT checks are mandatory?
- Where are per-workspace JUnit reports written?
- How are timeout, infrastructure failure, and assertion failure separated?
- What evidence must a worker include in `report_work`?
- Who owns `verify_run`, `gate_decision`, and promotion?

Recommendation:

Every contract issue should have a deterministic verification command and a
machine-readable report. A timeout must be reported as a timeout, not as a
failed product test. The orchestrator should not promote a contract change
based only on a local green command or a frontend mock result.

## Project-wide optimization workstreams

### Workstream A — Contract inventory and conflict register

Create a table of every cross-boundary contract currently used by the active
CadenceLMS issues:

- route and method;
- current implementation;
- proposed registry entry;
- frontend consumer;
- MSW/mock representation;
- generated artifact;
- owning issue/goal;
- conflict status;
- decision owner.

The first inventory must cover at least:

- `/auth/me`;
- `PATCH /auth/context`;
- notification list;
- notification mark-one-read;
- notification mark-all-read;
- unread count;
- notification preferences;
- enrollment-created event;
- notification persistence and delivery states;
- root contract export and verification commands.

Deliverable: one conflict register with no unresolved duplicate entries hidden
inside separate issue descriptions.

### Workstream B — Decision records and canonical schemas

For every conflict, record an ADR or contract decision that states:

- chosen shape;
- rejected alternatives;
- compatibility/migration requirement;
- affected issues;
- verification requirement;
- decision owner and date.

The notification route conflict and workspace/session context contract should be
the first decisions because they block the current product plans.

Deliverable: agreed contracts are represented in the orchestrator contract
registry and linked from the affected issues.

### Workstream C — Shared error, pagination, and lifecycle conventions

Define reusable conventions instead of solving them per feature:

- error envelope;
- validation error fields;
- authentication and authorization status codes;
- collection response shape;
- cursor pagination fields;
- timestamps and timezone representation;
- IDs and enum evolution;
- read versus delivery lifecycle;
- idempotency and retry behavior.

Deliverable: a compact contract conventions document or ADR that workers can
consult without reopening the same design discussion.

### Workstream D — Generation and verification pipeline

Establish:

1. one canonical contract source;
2. one clean-checkout generation command;
3. one root `contracts:verify` command;
4. artifact freshness and parseability checks;
5. frontend/type declaration compatibility checks;
6. per-workspace JUnit output;
7. correct timeout classification;
8. CI enforcement.

This workstream absorbs the underlying intent of #2, #3, #24, #25, and #31,
while keeping recovery and re-reporting of failed issues under coordinator
control.

Deliverable: a clean checkout can generate and verify contract artifacts, and
intentional drift causes a nonzero, diagnosable result.

### Workstream E — Consumer migration

After decisions are agreed, migrate consumers in dependency order:

```text
canonical schema
  → backend route/event implementation
  → frontend endpoint registry
  → MSW handlers and fixtures
  → UI consumers
  → integration/UAT journeys
```

No consumer should silently support both competing shapes unless a documented
migration period explicitly requires it. Compatibility code without an expiry
date creates permanent ambiguity.

Deliverable: each migrated consumer links to the agreed contract and passes its
targeted verification gate.

## Recommended implementation sequence

### Stage 0 — Freeze ambiguous feature expansion

Keep notification Goals 8 and 9 paused while their route and DTO conflicts are
unresolved. Do not allow new frontend handlers or API fallbacks to expand the
contract surface.

Keep workspace selector implementation blocked until `/auth/me` and
`PATCH /auth/context` are agreed.

### Stage 1 — Recover and stabilize contract verification

Address the existing failed verification work through the audited recovery
path:

- #2 contract export generation;
- #3 web contract verifier migration;
- #31 JUnit reporting and verification evidence.

Use the senior agent’s existing SHA evidence where it is still valid. Separate
the 300-second timeout false negative from actual test failures. Establish the
root `contracts:verify` behavior before adding more contract-dependent feature
work.

### Stage 2 — Decide the cross-stack contracts

Resolve and record:

- notification route family and DTOs;
- individual/read-all semantics;
- event envelope and idempotency;
- `/auth/me` workspace representation;
- `/auth/context` request/response and session persistence;
- shared errors and pagination.

### Stage 3 — Canonicalize and enforce

Generate artifacts, validate them from a clean checkout, align frontend
registries and MSW handlers, and enforce drift detection in CI.

### Stage 4 — Resume feature implementation

Release bounded leaf issues in dependency order:

- notification event and producer path;
- workspace backend and selector;
- notification feed and preferences;
- dispatch, digest, and additional event producers.

Each issue must point to the agreed contract record and include a targeted
verification command.

## Worker task template after optimization

Use this structure for GPT-5.4-mini assignments:

```text
Objective:
  One implementation result, stated in one sentence.

Contract:
  Link/identifier of the agreed contract or ADR.

Scope:
  Allowed files/packages and explicit exclusions.

Existing pattern:
  Comparable implementation to follow.

Acceptance:
  Observable behavior and required edge cases.

Verification:
  Exact command(s), expected report, and timeout class.

Dependencies:
  Issues/contracts that must already be complete.

Report:
  SHA, changed files, commands run, results, and known limitations.
```

Example of an appropriately optimized task:

```text
Implement GET /users/me/notifications using Contract C-NOTIFY-001.

Scope:
- backend notification route and its focused integration test;
- no frontend, route renaming, schema redesign, or worker changes.

Acceptance:
- authenticated user receives only their own notifications;
- unread filter works;
- cursor pagination follows the contract;
- 401 and validation errors use the shared error envelope.

Verification:
- run the named backend test workspace;
- run its typecheck;
- report the JUnit path and commit SHA.
```

## Gates for the contract optimization project

The project should not be marked complete until all of the following are true:

- every active cross-stack contract has an owner and canonical source;
- notification and workspace conflicts have explicit decisions;
- generated artifacts have one source and one reproducible generation path;
- `contracts:verify` works from a clean checkout;
- deliberate contract drift fails with a useful diagnostic;
- backend, frontend, and MSW consumers use the agreed route and DTO shapes;
- shared error, pagination, auth, event, and lifecycle rules are documented;
- verification distinguishes assertion failures from timeout/infrastructure
  failures;
- CI enforces the contract and typecheck gates;
- at least one notification and one workspace vertical slice pass targeted
  pseudo-production acceptance.

## Expected return on investment

This project reduces rework in three ways:

1. **Fewer conflicting implementations.** Workers receive a settled interface
   instead of choosing between live and proposed routes.
2. **More reliable delegation.** GPT-5.4-mini can handle bounded leaf issues
   with clear acceptance criteria and verification commands.
3. **More trustworthy promotion.** The orchestrator can distinguish a real
   product defect from stale artifacts, a mock mismatch, or a harness timeout.

The practical result is not merely cleaner documentation. It is a system where
backend, frontend, tests, CI, and pseudo-production are proving the same
behavior against the same contracts.

## Coordinator next actions

1. Create or update the contract conflict register from the active issue set.
2. Record decisions for notification routes/DTOs and workspace/session context.
3. Recover #2, #3, and #31 with explicit evidence and timeout classification.
4. Establish the canonical artifact generation and root verification commands.
5. Link all dependent issues to the decisions and add the worker task template
   fields before re-teaming implementation work.
6. Resume notification and workspace feature goals only after their required
   contract gates pass.
