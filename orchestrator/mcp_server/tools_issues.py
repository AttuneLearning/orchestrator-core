"""Issue MCP tools — list / get / claim / update_state / gate_decision /
create_subissue / append_log. Thin wrappers over repository.py."""

from __future__ import annotations

from dataclasses import asdict
import re
import subprocess
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..apply.npm_deps import ensure_deps_current
from ..config import load_settings
from ..pipelines import load_pipelines
from ..state_machine import apply_gate_decision
from ..workflow.escalation import make_escalation_cb
from ..workflow.loader import load_effective, load_permissions
from ..workflow.runner import run_step

_SHA_RE = re.compile(r"^[0-9a-fA-F]{6,40}$")
_STUB_MARKERS = (
    "stub code provider",
    "stub provider output",
    "notimplementederror",
    "placeholder output",
    "placeholder implementation",
)
# G7 output-quality lints on the implementation diff.
_PLACEHOLDER_TEST_RE = re.compile(r"expect\(\s*true\s*\)\s*\.\s*toBe\(\s*true\s*\)")
_RAW_FETCH_RE = re.compile(r"(?<![\w.])fetch\(")
_WEB_COMPONENT_RE = re.compile(r"^\+\+\+ b/apps/web/src/(pages|widgets|features|entities|processes)/")
_GIT_ID = ["-c", "user.email=orchestrator@local", "-c", "user.name=orchestrator",
           "-c", "commit.gpgsign=false"]

# GAP-1 lane enforcement: which paths each team's diff may touch. Contracts are
# backend-owned (ADR-DEV-001); frontend consumes them read-only and requests
# changes via contract_propose/comms (ADR-DEV-002). Root/toolchain files are
# senior-only. Teams not listed (senior, cloud, ...) are unrestricted.
_TEAM_LANES: dict[str, tuple[str, ...]] = {
    "backend": ("apps/api/", "packages/contracts/", "contracts.seed.json"),
    "frontend": ("apps/web/",),
}


def _out_of_lane(team: str, files: list[str]) -> list[str]:
    lanes = _TEAM_LANES.get(team or "")
    if not lanes:
        return []
    return [f for f in files
            if not any(f == lane or f.startswith(lane) for lane in lanes)]


def _valid_issue_branch(issue_id: int, branch: str) -> bool:
    return branch == f"issue-{issue_id}"


# G4: an issue is "ready" (claimable at the implementation gate) only if its spec
# is actionable — it names a target lane/file, a contract/ADR, or acceptance
# criteria. A weak model can't recover a vague issue; catch it before the claim.
_READY_SIGNAL_RE = re.compile(
    r"(apps/|packages/|[\w./-]+\.[a-z]{2,4}\b|accept|criteria|should |must |verify|"
    r"\btest\b|endpoint|route|contract|ADR-|component|schema|migration)", re.I)


def _issue_ready(title: str, description: str) -> tuple[bool, str]:
    desc = (description or "").strip()
    if len(desc) < 30:
        return False, ("description too thin to be actionable — state the ONE "
                       "deliverable, target file(s), and acceptance criteria")
    if not _READY_SIGNAL_RE.search(f"{title or ''} {desc}"):
        return False, ("no actionable signal (target file/lane, contract/ADR, or "
                       "acceptance criteria) — sharpen the spec before working it")
    return True, ""


def _git(repo_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo_path, *_GIT_ID, *args],
                          capture_output=True, text=True, timeout=30)


def _verify_commit_real(settings, issue_id: int, sha: str, team: str = "") -> None:
    """G1/G7/GAP-1 harness gate: prove an implementation report points at REAL,
    IN-LANE code the model actually committed — not a hallucinated sha, an
    empty/placeholder diff, or work smeared outside the team's write scope.

    Checks, against the shared git dir (promote/apply repo): the `issue-<id>`
    branch exists, the reported sha is on it, the diff vs the base branch is
    non-empty, and every changed file is inside the team's lane (_TEAM_LANES;
    senior/unlisted teams unrestricted). Then lints the ADDED lines: rejects
    placeholder tests (`expect(true).toBe(true)`) and raw `fetch(` in a web
    component (must go through the contract http client). Skipped only when no
    repo is configured (hermetic unit tests). Raises ValueError with a
    machine-actionable reason."""
    base = settings.promote_repo_path or settings.apply_repo_path
    if not base:
        return  # unconfigured (hermetic tests) — nothing to verify against
    branch = f"issue-{issue_id}"
    target = settings.promote_branch or "main"
    if _git(base, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        raise ValueError(
            f"issue {issue_id}: branch '{branch}' does not exist — no real commit to verify")
    if sha and _git(base, "merge-base", "--is-ancestor", sha, branch).returncode != 0:
        raise ValueError(
            f"issue {issue_id}: reported sha {sha[:10]} is not on '{branch}' — bogus report")
    # G10 (ADR-DEV-007): the issue branch must carry current `main`. A branch that
    # is BEHIND main was forked from a stale main (or never merged main in) and is
    # missing landed contracts/config — how workers build against gone-stale shapes
    # and go off-rails (e.g. the stale issue-268 branch). Require main's tip to be an
    # ancestor of the issue branch. (A fresh `git checkout -B issue-<id> main` passes;
    # a diverged/stale branch fails until the worker merges main in.)
    if _git(base, "merge-base", "--is-ancestor", target, branch).returncode != 0:
        raise ValueError(
            f"issue {issue_id}: branch '{branch}' is behind '{target}' — it was branched "
            f"from a stale {target} (missing landed contracts/config). Run "
            f"`git merge --no-edit {target}` (or rebranch: `git checkout -B {branch} "
            f"{target}`), re-commit, and report again (ADR-DEV-007)")
    diff = _git(base, "diff", f"{target}...{branch}").stdout
    if not diff.strip():
        raise ValueError(
            f"issue {issue_id}: '{branch}' has no diff vs '{target}' — empty/no real code")
    files = _git(base, "diff", "--name-only", f"{target}...{branch}").stdout.split()
    stray = _out_of_lane(team, files)
    if stray:
        lanes = ", ".join(_TEAM_LANES.get(team, ()))
        raise ValueError(
            f"issue {issue_id}: diff touches files outside the {team} lane "
            f"({lanes}): {', '.join(stray[:8])} — revert them; if the change is "
            f"genuinely needed there, it belongs to another team's issue "
            f"(contracts: use contract_propose) or the senior lane")
    added, cur_web = [], False
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur_web = bool(_WEB_COMPONENT_RE.match(line)) and "shared/api" not in line
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line)
            if cur_web and _RAW_FETCH_RE.search(line):
                raise ValueError(
                    f"issue {issue_id}: web component uses raw fetch() — call the contract "
                    f"http client (shared/api/*Api.ts → apiRequest), per ADR-DEV-002")
    if _PLACEHOLDER_TEST_RE.search("\n".join(added)):
        raise ValueError(
            f"issue {issue_id}: diff adds a placeholder test (expect(true).toBe(true)) — "
            f"write a real assertion against the unit under test (ADR-DEV-003)")
    return files


def _agent_stamp(pool: ConnectionPool, issue) -> dict[str, Any]:
    """GAP-5 telemetry: which agent/runtime produced this event — stamped
    server-side from the issue's assignment so it cannot be spoofed or omitted."""
    stamp: dict[str, Any] = {}
    if issue.assigned_agent is not None:
        stamp["agent_id"] = issue.assigned_agent
        agent = repo.get_agent(pool, issue.assigned_agent)
        if agent is not None:
            stamp["agent_runtime"] = agent.runtime
            stamp["agent_team"] = agent.team
    return stamp


def _has_machine_verification(pool: ConnectionPool, issue_id: int) -> bool:
    """GAP-4: newest harness-recorded verify result since the last directive.
    True only if verify_run itself recorded returncode==0 — a worker's
    self-reported tests_passed does not count."""
    for event in repo.recent_events(pool, issue_id, limit=50):
        if event.event_type == "directive":
            break
        if event.event_type != "tests_run":
            continue
        payload = event.payload or {}
        if payload.get("machine"):
            return payload.get("returncode") == 0
    return False


def _looks_like_stub(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(k, ""))
        for k in ("summary", "diff", "content", "provider", "model")
    ).lower()
    return any(marker in text for marker in _STUB_MARKERS)


def _validate_report_work_payload(
    issue_id: int,
    gate_type: str | None,
    sha: str,
    branch: str,
    tests_passed: Optional[bool],
    payload: dict[str, Any],
) -> None:
    if gate_type != "implementation":
        return
    if not _valid_issue_branch(issue_id, branch):
        raise ValueError(
            f"implementation report for issue {issue_id} must use branch "
            f"'issue-{issue_id}'"
        )
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(
            f"implementation report for issue {issue_id} must include a commit sha"
        )
    if tests_passed is not True:
        raise ValueError(
            f"implementation report for issue {issue_id} must set tests_passed=true"
        )
    if _looks_like_stub(payload):
        raise ValueError(
            f"implementation report for issue {issue_id} appears to be stub output"
        )


def _has_valid_implementation_report(pool: ConnectionPool, issue_id: int) -> bool:
    for event in repo.recent_events(pool, issue_id, limit=50):
        if event.event_type == "directive":
            break
        if event.event_type != "code_committed":
            continue
        payload = event.payload or {}
        try:
            _validate_report_work_payload(
                issue_id,
                "implementation",
                str(payload.get("sha", "")),
                str(payload.get("branch", "")),
                payload.get("tests_passed"),
                payload,
            )
        except ValueError:
            continue
        return True
    return False


def _strip_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop run_step's `action`/`verdict` envelope keys, leaving the bare
    execution detail dict (e.g. a builtin's `{ok, reason, installed, ...}` or
    `_run_shell`'s `{ok, reason, returncode, stdout, stderr}`) — this is what
    lets the enabled path reuse the legacy payload shapes verbatim."""
    return {k: v for k, v in payload.items() if k not in ("action", "verdict")}


def _action_label(payload: dict[str, Any]) -> str:
    info = payload.get("action") or {}
    return str(info.get("run") or info.get("builtin") or "<unnamed action>")


_BLOCKED_NOTE = "approval pending on /actions; re-run after approval"


def _verify_run_enabled(
    pool: ConnectionPool, settings: Any, issue: Any, issue_id: int,
    wt: str, verify_branch: str,
) -> dict[str, Any]:
    """`workflow_profile: enabled` path for `verify_run` (WP-08/WP-13/WP-18).

    Runs the `services` (if declared), `prepare`, then `verify` steps of the
    effective workflow Profile (Phase A/D: `orchestrator.workflow.loader`/
    `runner`) in place of the inline `ensure_deps_current` call + hardcoded
    `npm run typecheck && npm test`. Services readiness gates the build (plan
    step 4 acceptance: "verify fails fast with a clear message if a required
    service is down") — a `services` step with no actions (no services
    declared) is a no-op, and the function proceeds straight to `prepare`.
    A down default/engine-declared probe fails the step immediately (see
    `_services_escalation_cb`) rather than parking on the approval queue; only
    a repo-authored probe (source == "repo") can produce `blocked_on_approval`
    for this step, same as any other custom action.

    Emits the SAME event kinds/payload shapes the legacy path emits for the
    same outcomes (`deps_reinstalled`, `tests_run`) so downstream consumers
    (gate decisions, `_has_machine_verification`, dashboards) see no
    difference on the happy/fail paths. A `blocked_on_approval` outcome from
    any step never becomes a failed `tests_run` event — instead the real
    persistence (WP-12's `escalation.make_escalation_cb`, wired below, calling
    through to `repository.create_pending_action`) records the ONE
    `action_escalated` event for a NEW pending row; this function's own
    `event_cb` only builds the `blocked` dict used for the return payload, it
    never appends the event itself (a re-polled call that hits an existing
    pending row correctly emits no duplicate). Returns `passed=None` with
    `step`/`action`/`phase`/`note` (WP-13) so a caller knows exactly what is
    pending and what to do about it. The issue's state/retry counters are
    never touched on this path — a pending approval is "not attempted", not a
    failure (plan §2.2).
    """
    perms = load_permissions(settings)
    profile = load_effective(settings, wt, role="qa")
    requested_by = f"verify_run:{issue.team or ''}"

    services_detail: dict[str, Any] = {}
    services_blocked: dict[str, Any] = {}
    services_phase_seen: dict[str, str] = {}
    services_escalation_base = make_escalation_cb(pool, issue_id, wt, "services", requested_by)

    def _services_cb(kind: str, payload: dict[str, Any]) -> None:
        nonlocal services_detail, services_blocked
        if kind in ("executed", "failed", "refused"):
            services_detail = _strip_envelope(payload)
        elif kind == "escalated":
            services_blocked = {"step": "services", "action": _action_label(payload),
                                 "phase": services_phase_seen.get("phase", "authorize")}

    def _services_escalation_cb(action: Any, phase: str) -> str:
        # A down default/engine-declared service (mongo/redis/...) is an
        # environment problem, not an approval question — plan step 4's
        # acceptance is "verify fails fast with a clear message", not "wait
        # on the /actions queue". Deny immediately so run_step turns this
        # into a real "failed" StepResult; only a repo-authored probe
        # (source == "repo") goes through the normal pending-approval flow,
        # same as any other custom action.
        if action.source != "repo":
            return "denied"
        services_phase_seen["phase"] = phase
        return services_escalation_base(action, phase)

    if profile.step("services").actions_for("qa"):
        services_result = run_step(wt, profile, "services", "qa", perms,
                                    event_cb=_services_cb, escalation_cb=_services_escalation_cb)

        if services_result.status == "blocked_on_approval":
            return {
                "passed": None, "status": "blocked_on_approval",
                "step": services_blocked.get("step", "services"),
                "action": services_blocked.get("action", services_result.reason),
                "phase": services_blocked.get("phase", services_phase_seen.get("phase", "authorize")),
                "note": _BLOCKED_NOTE,
            }

        if services_result.status == "failed":
            reason = services_detail.get("reason") or services_result.reason
            message = f"service not ready: {reason}"
            payload = {
                "machine": True, "cmd": "services",
                "returncode": 1, "duration_s": 0.0, "branch": verify_branch,
                "stdout_tail": "", "stderr_tail": message[-800:],
                "deps": services_detail,
            }
            payload.update(_agent_stamp(pool, issue))
            repo.append_log(pool, issue_id, "tests_run", payload)
            return {"passed": False, "returncode": 1, "duration_s": 0.0, "tail": message}

    prepare_detail: dict[str, Any] = {}
    blocked: dict[str, Any] = {}
    prepare_phase_seen: dict[str, str] = {}
    prepare_escalation_base = make_escalation_cb(pool, issue_id, wt, "prepare", requested_by)

    def _prepare_cb(kind: str, payload: dict[str, Any]) -> None:
        nonlocal prepare_detail, blocked
        if kind in ("executed", "failed", "refused"):
            prepare_detail = _strip_envelope(payload)
            if kind == "executed" and prepare_detail.get("installed"):
                repo.append_log(pool, issue_id, "deps_reinstalled",
                                {"machine": True, "branch": verify_branch, **prepare_detail})
        elif kind == "escalated":
            # The `action_escalated` event is owned by repository.create_pending_action
            # (called via the escalation_cb -> escalation.handle_escalation, below) —
            # it fires once per NEW pending row, so a re-polled call that hits an
            # existing pending row emits no duplicate event. Only the return payload
            # (built from `blocked`) is this callback's job now.
            blocked = {"step": "prepare", "action": _action_label(payload),
                       "phase": prepare_phase_seen.get("phase", "authorize")}

    def _prepare_escalation_cb(action: Any, phase: str) -> str:
        prepare_phase_seen["phase"] = phase
        return prepare_escalation_base(action, phase)

    prepare_result = run_step(wt, profile, "prepare", "qa", perms,
                              event_cb=_prepare_cb, escalation_cb=_prepare_escalation_cb)

    if prepare_result.status == "blocked_on_approval":
        return {
            "passed": None, "status": "blocked_on_approval",
            "step": blocked.get("step", "prepare"),
            "action": blocked.get("action", prepare_result.reason),
            "phase": blocked.get("phase", prepare_phase_seen.get("phase", "authorize")),
            "note": _BLOCKED_NOTE,
        }

    if prepare_result.status == "failed":
        returncode = prepare_detail.get("returncode", 1)
        payload = {
            "machine": True, "cmd": prepare_detail.get("cmd") or "prepare",
            "returncode": returncode, "duration_s": 0.0, "branch": verify_branch,
            "stdout_tail": "", "stderr_tail": (prepare_detail.get("stderr_tail") or "")[-800:],
            "deps": prepare_detail,
        }
        payload.update(_agent_stamp(pool, issue))
        repo.append_log(pool, issue_id, "tests_run", payload)
        return {"passed": False, "returncode": returncode, "duration_s": 0.0,
                "tail": f"dependency install failed before verify: {prepare_result.reason}"}

    verify_detail: dict[str, Any] = {}
    verify_phase_seen: dict[str, str] = {}
    verify_escalation_base = make_escalation_cb(pool, issue_id, wt, "verify", requested_by)

    def _verify_cb(kind: str, payload: dict[str, Any]) -> None:
        nonlocal verify_detail, blocked
        if kind in ("executed", "failed", "refused"):
            verify_detail = _strip_envelope(payload)
            verify_detail["_action"] = payload.get("action") or {}
        elif kind == "escalated":
            # See the matching comment in _prepare_cb: create_pending_action is the
            # sole emitter of `action_escalated` — no tool-level append here.
            blocked = {"step": "verify", "action": _action_label(payload),
                       "phase": verify_phase_seen.get("phase", "authorize")}

    def _verify_escalation_cb(action: Any, phase: str) -> str:
        verify_phase_seen["phase"] = phase
        return verify_escalation_base(action, phase)

    started = time.monotonic()
    verify_result = run_step(wt, profile, "verify", "qa", perms,
                             event_cb=_verify_cb, escalation_cb=_verify_escalation_cb)
    duration = round(time.monotonic() - started, 1)

    if verify_result.status == "blocked_on_approval":
        return {
            "passed": None, "status": "blocked_on_approval",
            "step": blocked.get("step", "verify"),
            "action": blocked.get("action", verify_result.reason),
            "phase": blocked.get("phase", verify_phase_seen.get("phase", "authorize")),
            "note": _BLOCKED_NOTE,
        }

    action_info = verify_detail.pop("_action", {})
    cmd = action_info.get("run") or action_info.get("builtin") or ""
    returncode = verify_detail.get("returncode")
    out = verify_detail.get("stdout", "") or ""
    err = verify_detail.get("stderr", "") or ""

    # If status is "ok" but a verify action actually failed (ok=False with on_fail: warn),
    # derive returncode from the failed action's result, not from the overall status.
    if verify_result.status == "ok":
        for result in verify_result.results:
            if not result.ok and not result.skipped:
                failed_detail = result.detail
                returncode = failed_detail.get("returncode", 1)
                out = failed_detail.get("stdout", "") or ""
                err = failed_detail.get("stderr", "") or ""
                cmd = result.action.run or result.action.builtin or ""
                break

    # For denied actions (which never execute), ensure cmd names what was denied
    # and include the denial reason in stderr so the persisted event is self-naming (WP-19 NIT).
    if verify_result.status == "failed" and not cmd and verify_result.results:
        last_result = verify_result.results[-1]
        cmd = last_result.action.run or last_result.action.builtin or ""
        if not err:
            err = verify_result.reason  # e.g. "denied: custom-command"

    if returncode is None:
        returncode = 1 if verify_result.status == "failed" else 0
    payload = {
        "machine": True, "cmd": cmd, "returncode": returncode,
        "duration_s": duration, "branch": verify_branch,
        "stdout_tail": out[-1200:], "stderr_tail": err[-800:],
        "deps": prepare_detail,
    }
    payload.update(_agent_stamp(pool, issue))  # GAP-5 telemetry
    repo.append_log(pool, issue_id, "tests_run", payload)
    # Fall back to the StepResult's own reason when there's no stdout/stderr to
    # show (e.g. a denied escalation never executes, so there's no process
    # output) — WP-13 (c): a denial must be a REAL failure that names itself,
    # not a silent blank tail.
    tail = out[-600:] or err[-600:] or (verify_result.reason if verify_result.status == "failed" else "")
    return {"passed": returncode == 0, "returncode": returncode,
            "duration_s": duration, "tail": tail}


def register(mcp: FastMCP, pool: ConnectionPool, settings=None) -> None:
    # Accept the caller's (instance-resolved) settings so the G1 commit check runs
    # against the RIGHT repo; fall back to load_settings() for standalone use.
    settings = settings if settings is not None else load_settings()
    pipelines = load_pipelines(settings.pipelines)

    @mcp.tool()
    def list_issues(goal_id: Optional[int] = None,
                    state: Optional[str] = None) -> list[dict[str, Any]]:
        """List issues, optionally filtered by goal_id and/or state."""
        states = [state] if state else None
        return [asdict(i) for i in repo.list_issues(pool, goal_id=goal_id, states=states)]

    @mcp.tool()
    def get_issue(issue_id: int) -> Optional[dict[str, Any]]:
        """Fetch a single issue by id."""
        issue = repo.get_issue(pool, issue_id)
        return asdict(issue) if issue else None

    @mcp.tool()
    def claim_issue(issue_id: int, agent_id: int) -> dict[str, Any]:
        """Assign an issue to an agent (marks the agent busy). G4: an under-specified
        implementation issue is FLAGGED for the lead to sharpen (a 'readiness_warning'
        event) but not hard-blocked — mechanical readiness over-fires on valid prose
        specs, so a block would stall real work. The flag is the signal; the real
        safety net is the G1 commit gate + G8 escalation downstream."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        if issue.gate_type == "implementation":
            ready, why = _issue_ready(issue.title, issue.description)
            if not ready:
                repo.append_log(pool, issue_id, "readiness_warning", {"reason": why})
        repo.claim_issue(pool, issue_id, agent_id)
        result = asdict(repo.get_issue(pool, issue_id))
        # Reservation-time branch mandate (ADR-DEV-007 / G10): tell the worker the
        # one branch this issue may be built on, and how to create it, so it starts
        # on a correct fresh branch instead of a shared/stale one.
        if issue.gate_type == "implementation":
            target = (settings.promote_branch if settings and settings.promote_branch
                      else "main")
            result["branch"] = f"issue-{issue_id}"
            result["checkout"] = f"git checkout -B issue-{issue_id} {target}"
            result["next"] = (f"check out issue-{issue_id} from {target}, then "
                              f"confirm_branch({issue_id}) BEFORE implementing (one issue "
                              f"per branch — ADR-DEV-007)")
        return result

    @mcp.tool()
    def confirm_branch(issue_id: int) -> dict[str, Any]:
        """Reservation-time branch check (ADR-DEV-007 / G10). Call this RIGHT AFTER
        `git checkout -B issue-<id> <main>` and BEFORE implementing. The harness
        verifies — from git metadata only, it never touches your worktree — that
        branch `issue-<id>` exists AND carries current main, so you fail FAST if you're
        on a stale/wrong branch instead of wasting a whole cycle. Records
        `branch_confirmed`. Returns {ok:true, branch} or {ok:false, reason, fix}. This
        is a fast-fail helper; the un-bypassable gate is still report_work (G10)."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        branch = f"issue-{issue_id}"
        base = (settings.promote_repo_path or settings.apply_repo_path) if settings else ""
        target = (settings.promote_branch if settings and settings.promote_branch else "main")
        recreate = f"git checkout -B {branch} {target}"
        if not base:
            return {"ok": True, "branch": branch, "note": "no repo configured — skipped"}
        if _git(base, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
            return {"ok": False, "branch": branch,
                    "reason": f"branch '{branch}' does not exist — create it from {target}",
                    "fix": recreate}
        if _git(base, "merge-base", "--is-ancestor", target, branch).returncode != 0:
            return {"ok": False, "branch": branch,
                    "reason": (f"branch '{branch}' is behind '{target}' — you'd build on "
                               f"stale contracts/config"),
                    "fix": f"git merge --no-edit {target}   (or rebranch: {recreate})"}
        repo.append_log(pool, issue_id, "branch_confirmed", {"branch": branch})
        return {"ok": True, "branch": branch}

    @mcp.tool()
    def update_state(issue_id: int, to_state: str,
                     gate_type: Optional[str] = None) -> dict[str, Any]:
        """Transition an issue to a new state (writes a matching event)."""
        return asdict(repo.update_state(pool, issue_id, to_state, gate_type=gate_type))

    @mcp.tool()
    def gate_decision(issue_id: int, passed: bool, reasons: Optional[list[str]] = None) -> dict[str, Any]:
        """Record a gate_review decision and advance the issue accordingly."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        if passed and issue.gate_type == "implementation":
            if not _has_valid_implementation_report(pool, issue_id):
                raise ValueError(
                    f"issue {issue_id} cannot pass implementation without a valid "
                    f"code_committed report on branch 'issue-{issue_id}'"
                )
            # G1/G7/GAP-1: branch/diff/lints/lane must all hold
            _verify_commit_real(settings, issue_id, "", team=issue.team or "")
        if (passed and issue.gate_type == "verification"
                and settings.verify_worktrees.get(issue.team or "")):
            # GAP-4: where a verify worktree is configured, a pass requires the
            # HARNESS to have run the checks (verify_run, machine-recorded exit 0);
            # a QA worker's self-reported pass is not evidence.
            if not _has_machine_verification(pool, issue_id):
                raise ValueError(
                    f"issue {issue_id} cannot pass verification without a machine-"
                    f"recorded green verify_run — call verify_run({issue_id}) first")
        pipeline = pipelines[issue.pipeline]
        gate = pipeline.gate(issue.gate_type)
        outcome = apply_gate_decision(
            pipeline, gate, passed=passed, retry_count=issue.retry_count,
            retry_cap=settings.thresholds.retry_cap,
            triggered_by_message=issue.triggered_by_message,
        )
        payload = {"reasons": reasons or []}
        payload.update(_agent_stamp(pool, issue))  # GAP-5 telemetry
        if issue.gate_type == "e2e" and passed:
            from ..backup import record_backup
            payload["database_backup"] = record_backup(
                pool, settings,
                reason=f"after-e2e-issue-{issue.id}",
                issue_id=issue.id,
                goal_id=issue.goal_id,
            )
        updated = repo.update_state(
            pool, issue_id, outcome.state, gate_type=outcome.gate_type,
            event_type=outcome.event_type, payload=payload,
            retry_count=outcome.retry_count,
        )
        return asdict(updated)

    @mcp.tool()
    def create_subissue(parent_id: int, title: str, description: str = "") -> dict[str, Any]:
        """Create a child issue under a parent (inherits goal, depth+1)."""
        parent = repo.get_issue(pool, parent_id)
        if parent is None:
            raise ValueError(f"no parent issue {parent_id}")
        return asdict(repo.create_subissue(pool, parent, title, description))

    @mcp.tool()
    def apply_directive(issue_id: int, note: str = "",
                        actor: str = "human") -> dict[str, Any]:
        """Un-quarantine an off_rails issue (human directive; resets counters)."""
        return asdict(repo.apply_directive(pool, issue_id, "resume",
                                           note=note, actor=actor))

    @mcp.tool()
    def append_log(issue_id: int, event_type: str,
                   payload: Optional[dict[str, Any]] = None) -> dict[str, str]:
        """Append a non-transition event to an issue's log."""
        repo.append_log(pool, issue_id, event_type, payload or {})
        return {"status": "ok"}

    @mcp.tool()
    def heartbeat(agent_id: int) -> dict[str, Any]:
        """Pull workers call this every poll: refreshes last_seen (so the engine's
        liveness reclaim doesn't treat them as dead), reactivates a reclaimed
        worker (offline -> idle), and returns the live loop policy so the worker
        self-paces. `next_poll_seconds` is 0 when looping is disabled, meaning
        'stop after the queue drains'; otherwise it's the idle poll cadence."""
        agent = repo.get_agent(pool, agent_id)
        if agent is None:
            # No such agent in THIS coordinator's DB — almost always means the MCP
            # server is pointed at the wrong database. Signal it clearly instead of
            # silently reporting loop_enabled=False (which reads as "looping disabled").
            return {
                "status": "unknown_agent",
                "agent_id": agent_id,
                "note": ("no agent %d in this coordinator — the MCP server is likely "
                         "connected to the wrong database. Launch it with "
                         "`--instance <project>` (e.g. tendcharting)." % agent_id),
                "loop_enabled": False,
                "next_poll_seconds": 0,
            }
        # Read status BEFORE touch: touch_agent itself now revives an offline worker
        # (offline -> idle), so reactivation must be observed from the pre-touch state.
        reactivated = agent.status == "offline"
        repo.touch_agent(pool, agent_id)  # refresh last_seen; revives offline -> idle
        loop_enabled = bool(agent.loop_enabled) if agent else False
        # Cooldown window (migration 0019): if paused_until is in the future, the
        # worker should sleep until then instead of polling; the engine also skips
        # assigning to it. pause_seconds is how long to sleep (0 = active).
        import datetime as _dt
        paused_until = agent.paused_until if agent else None
        pause_seconds = 0
        if paused_until is not None:
            pause_seconds = max(0, int((paused_until - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))
        return {
            "status": "ok",
            "reactivated": reactivated,
            "loop_enabled": loop_enabled,
            "next_poll_seconds": repo.agent_next_poll_seconds(agent) if agent else 0,
            "paused_until": paused_until.isoformat() if paused_until else None,
            "pause_seconds": pause_seconds,
        }

    @mcp.tool()
    def list_my_work(agent_id: int) -> list[dict[str, Any]]:
        """Issues currently assigned to this pull worker and awaiting its action
        (in_progress). One call for a poll loop to find what to do next. For the
        full picture (work + inbound messages) use my_queue."""
        repo.touch_agent(pool, agent_id)  # polling is a liveness signal (revives offline)
        return [
            asdict(i) for i in repo.list_issues(
                pool, states=["in_progress"], assigned_agent=agent_id)
        ]

    def _msg_item(m: dict[str, Any], kind_label: str) -> dict[str, Any]:
        # Every message carries its originating issue + source + thread link so
        # history is always discoverable from the queue.
        return {
            "id": m["id"], "type": kind_label,
            "subject": m["subject"], "body": m["body"],
            "source": m["from_team"], "issue_id": m.get("issue_id"),
            "reply_to": m.get("reply_to"), "priority": m.get("priority"),
            "read_at": m.get("read_at"), "created_at": m.get("created_at"),
        }

    @mcp.tool()
    def my_queue(agent_id: int) -> dict[str, Any]:
        """The master queue: everything on this agent's plate in one call.
        `work` = its assigned in-progress issues (same as list_my_work). `messages`
        = UNREAD inbound for its team — answers to questions it asked (type
        'answer') and any pending requests (type 'request'), each linked to its
        originating issue (issue_id), source (from_team) and thread (reply_to).
        Call mark_read(message_id) after consuming a message so it drops off."""
        repo.touch_agent(pool, agent_id)  # polling is a liveness signal (revives offline)
        agent = repo.get_agent(pool, agent_id)
        work = [asdict(i) for i in repo.list_issues(
            pool, states=["in_progress"], assigned_agent=agent_id)]
        messages: list[dict[str, Any]] = []
        if agent is not None:
            messages += [_msg_item(m, "answer")
                         for m in repo.list_responses(pool, to_team=agent.team,
                                                      unread_only=True)]
            messages += [_msg_item(m, "request")
                         for m in repo.pending_messages(pool, to_team=agent.team)]
        return {"work": work, "messages": messages}

    @mcp.tool()
    def mark_read(message_id: int) -> dict[str, str]:
        """Mark an inbox message consumed so it drops out of the next my_queue."""
        repo.mark_message_read(pool, message_id)
        return {"status": "ok"}

    @mcp.tool()
    def report_work(issue_id: int, sha: str = "", branch: str = "",
                    pr_url: str = "", tests_passed: Optional[bool] = None,
                    summary: str = "") -> dict[str, str]:
        """Record the code a pull worker produced (a `code_committed` event).
        Reports a pointer to the work in the worker's OWN repo — the orchestrator
        never holds or applies the code. The verdict gate consumes this as
        evidence. Call before gate_decision to advance the gate."""
        payload: dict[str, Any] = {"sha": sha}
        if branch:
            payload["branch"] = branch
        if pr_url:
            payload["pr_url"] = pr_url
        if tests_passed is not None:
            payload["tests_passed"] = tests_passed
        if summary:
            payload["summary"] = summary
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        _validate_report_work_payload(
            issue_id, issue.gate_type, sha, branch, tests_passed, payload,
        )
        payload.update(_agent_stamp(pool, issue))  # GAP-5 telemetry
        if issue.gate_type == "implementation":
            # G1/G7/GAP-1: real commit + lints + in-lane
            files = _verify_commit_real(settings, issue_id, sha, team=issue.team or "")
            # GAP-6 (soft): contract sources changed without the review seed —
            # flag for the lead; the in-repo contracts:sync guard is advisory only.
            if files and any(f.startswith("packages/contracts/") for f in files) \
                    and "contracts.seed.json" not in files:
                repo.append_log(pool, issue_id, "contract_sync_warning", {
                    "reason": "packages/contracts changed but contracts.seed.json did "
                              "not — run `npm run contracts:sync` if the API surface "
                              "changed (ADR-DEV-001)"})
        repo.append_log(pool, issue_id, "code_committed", payload)
        return {"status": "ok"}

    @mcp.tool()
    def verify_run(issue_id: int) -> dict[str, Any]:
        """GAP-4: the HARNESS runs verification for this issue and records the
        machine result — call this instead of running typecheck/tests yourself.

        Checks out `_verify-<id>` from `issue-<id>` in the team's configured verify
        worktree, executes the verify command (typecheck + tests), and appends a
        machine-stamped `tests_run` event with the real exit code. The verification
        gate only accepts a pass backed by this evidence. May take a few minutes —
        wait for it. Then call gate_decision(issue_id, passed=<returned passed>).

        If the result is `{"status": "blocked_on_approval", "passed": None, ...}`
        (workflow-profile mode only), a required action needs a human's OK on the
        dashboard's `/actions` queue first — this is NOT a failure: heartbeat and
        re-poll, then call verify_run(issue_id) again later (after approval) to
        resume; do NOT call gate_decision with a failing verdict for this outcome."""
        issue = repo.get_issue(pool, issue_id)
        if issue is None:
            raise ValueError(f"no issue {issue_id}")
        wt = settings.verify_worktrees.get(issue.team or "")
        if not wt:
            raise ValueError(
                f"no verify worktree configured for team '{issue.team}' — "
                f"set settings.verify_worktrees.{issue.team}")
        branch, verify_branch = f"issue-{issue_id}", f"_verify-{issue_id}"
        if _git(wt, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
            raise ValueError(f"issue {issue_id}: branch '{branch}' does not exist — "
                             f"nothing to verify")
        # The verify worktree is a throwaway checkout target (it never holds real
        # work — commits live on issue-<id>). Discard any residue from a prior or
        # interrupted verify run BEFORE switching branches: a dirty tree makes
        # `git checkout -B` abort with "local changes would be overwritten", which
        # otherwise permanently wedges every subsequent verify for the team until
        # the worktree is cleaned by hand. reset+clean (no -x: keep node_modules).
        if settings.workflow_profile == "enabled":
            # Profile-driven cleanup (Phase D of the Workflow Profile cutover) —
            # replaces the inline reset/clean below. Load the profile from the
            # worktree as-is (pre-checkout state); if profile loading fails, fall
            # back to defaults so cleanup always runs.
            profile = load_effective(settings, wt, role="qa")
            perms = load_permissions(settings)

            # Wire event_cb to log cleanup events (verify_run convention).
            cleanup_detail: dict[str, Any] = {}
            def _cleanup_cb(kind: str, payload: dict[str, Any]) -> None:
                nonlocal cleanup_detail
                if kind in ("executed", "failed", "refused"):
                    cleanup_detail = _strip_envelope(payload)

            cleanup_result = run_step(wt, profile, "cleanup", "qa", perms,
                                      event_cb=_cleanup_cb)

            if cleanup_result.status == "blocked_on_approval":
                return {"passed": False, "returncode": 1, "duration_s": 0.0,
                        "tail": f"cleanup blocked on approval: {cleanup_result.reason}"}

            if cleanup_result.status == "failed":
                return {"passed": False, "returncode": 1, "duration_s": 0.0,
                        "tail": f"cleanup failed: {cleanup_result.reason}"}
        else:
            # Legacy path: inline reset/clean (unchanged).
            _git(wt, "reset", "--hard")
            _git(wt, "clean", "-fd")

        co = _git(wt, "checkout", "-B", verify_branch, branch)
        if co.returncode != 0:
            raise ValueError(f"verify checkout failed: {co.stderr[:300]}")
        if settings.workflow_profile == "enabled":
            # Profile-driven prepare+verify (Phase A/B of the Workflow Profile
            # cutover) — replaces the inline ensure_deps_current + hardcoded
            # verify command below. `legacy` (default) never reaches here.
            return _verify_run_enabled(pool, settings, issue, issue_id, wt, verify_branch)
        # Reconcile node_modules with the freshly checked-out lockfile BEFORE typecheck.
        # `clean -fd` above keeps node_modules (no -x), so a dependency that landed on
        # main since the last install would otherwise surface as a spurious "Cannot find
        # module" failure that falsely bounces the issue. Only reinstalls on lockfile change.
        deps = ensure_deps_current(wt)
        if deps.get("installed"):
            repo.append_log(pool, issue_id, "deps_reinstalled",
                            {"machine": True, "branch": verify_branch, **deps})
        if not deps["ok"]:
            payload = {
                "machine": True, "cmd": "npm ci", "returncode": deps.get("returncode", 1),
                "duration_s": 0.0, "branch": verify_branch, "stdout_tail": "",
                "stderr_tail": (deps.get("stderr_tail") or "")[-800:], "deps": deps,
            }
            payload.update(_agent_stamp(pool, issue))
            repo.append_log(pool, issue_id, "tests_run", payload)
            return {"passed": False, "returncode": deps.get("returncode", 1),
                    "duration_s": 0.0,
                    "tail": f"dependency install failed before verify: {deps['reason']}"}
        cmd = settings.verify_cmd or "npm run typecheck && npm test"
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True,
                                  text=True, timeout=900)
            returncode, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = f"verify timed out after 900s"
        duration = round(time.monotonic() - started, 1)
        payload = {
            "machine": True, "cmd": cmd, "returncode": returncode,
            "duration_s": duration, "branch": verify_branch,
            "stdout_tail": out[-1200:], "stderr_tail": err[-800:],
            "deps": deps,
        }
        payload.update(_agent_stamp(pool, issue))  # GAP-5 telemetry
        repo.append_log(pool, issue_id, "tests_run", payload)
        return {"passed": returncode == 0, "returncode": returncode,
                "duration_s": duration, "tail": out[-600:] or err[-600:]}
