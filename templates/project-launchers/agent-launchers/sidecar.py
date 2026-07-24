#!/usr/bin/env python3
"""Durable-worker side-car (Phase 2, durable-worker-sidecar plan §4-5).

Architecture (see plans/durable-worker-sidecar-plan.md in the orchestrator
repo for the full picture):

    orchestrator (engine + dashboard)
       |  publishes cadence (loop_enabled, poll_interval_seconds), pause
       v
    side-car (this script, one per worker)          durable worker session
       - owns cadence (active window / dormant)  -->  (opencode serve, etc.)
       - injects "tick" prompts into the SAME session, never relaunches it
       - heartbeats the dashboard ~20s ALWAYS (token-free)
       - watchdog: restarts the session on crash / stuck / runaway tick
       - Ctrl-C exitable; leaves the worker alive unless told to kill it

The worker never sleeps or loops itself: it does one tick of work and
"yields" (the HTTP turn ends); the side-car alone decides when the next tick
fires. This is a single-threaded, non-blocking state machine: one `step(now)`
call per loop iteration. `now`, and every wait, comes from an injectable
clock/sleeper so the whole thing is unit-testable without real sleeps.

Runtime adapters implement `Adapter` (below): `OpencodeAdapter` (HTTP against
`opencode serve`, Phase 2) and `TmuxAdapter` (capture-pane/send-keys against a
claude/codex TUI pane, Phase 5). A `FakeAdapter` used for tests lives in
tests/test_sidecar.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Watchdog defaults: t_max must exceed the verify ceiling (engine
# verify_timeout_s 3000 / MCP client timeout 3300) or a pending verify_run
# reads as "stuck" and gets killed mid-verify (the 2026-07-21 false-negative
# bug). NEVER lower this without re-deriving from the ceiling.
#
# MAJOR 7 (gate review): the margin was 300s, too tight -- a tick is one
# issue plus a full verify_run, and that can run long after the verify
# ceiling itself (model think time, retries inside the turn, etc). Draining
# a backlog does NOT need more margin here: it happens ACROSS ticks via the
# READY-TO-CLEAR / clear() handshake, not by extending a single tick. Margin
# is now 1800s, giving MIN_T_MAX_S = 3300 + 1800 = 5100s.
#
# MINOR (opus re-review): MIN_T_MAX_S must EQUAL the derived floor, not a
# stale literal (it used to be a hardcoded 3600, left over from before the
# margin bump -- that made resolve_t_max's own error message self-
# contradictory: "below 3600s (ceiling 3300 + margin 1800)" adds up to
# 5100, not 3600). Deriving both from the same two constants keeps them
# from drifting apart again.
# --------------------------------------------------------------------------- #
VERIFY_CEILING_S = 3300
T_MAX_MARGIN_S = 1800
MIN_T_MAX_S = VERIFY_CEILING_S + T_MAX_MARGIN_S    # 5100
DEFAULT_T_MAX_S = MIN_T_MAX_S

# BLOCKER 3: how many CONSECUTIVE alive() probe failures are required before
# the watchdog restarts the worker. A single flaky probe must never abort an
# in-flight tick -- only a sustained run of failures indicates the worker is
# actually gone. (Exception: an owned subprocess that has confirmably exited
# -- see Adapter.owned_process_dead -- restarts immediately, no debounce.)
ALIVE_FAILURE_THRESHOLD = 3

# Live-tmux e2e finding (coordinator report): the watchdog's 3-strike debounce
# bounds how OFTEN a restart can be TRIGGERED, but nothing bounded how often
# one could actually FIRE -- a persistently-failing alive() probe (or a
# crash-looping TUI) restarted every ~3 probes, seconds apart. For a real
# claude/codex session each respawn replays the initial prompt from spawn_cmd
# (token burn + session churn), so an unbounded restart rate is a real cost,
# not just log noise. Sidecar._restart() enforces a minimum number of seconds
# between actual restarts; a restart that would fire within the cooldown is
# logged (RESTART_SUPPRESSED) and skipped WITHOUT resetting the failure
# counters that triggered it, so it fires as soon as the cooldown lapses
# rather than being lost. 0 disables the cooldown (immediate, pre-fix
# behavior) -- validated >= 0 at the CLI layer, see resolve_t_max's sibling
# check in main().
DEFAULT_RESTART_COOLDOWN_S = 120

# MAJOR 6: bounds enforced on dashboard-supplied cadence policy.
MIN_POLL_INTERVAL_S = 60
MAX_POLL_INTERVAL_S = 7200

# Phase 4 (plan §7, migration 0024): bounds enforced on the dashboard-supplied
# cadence-WINDOW overrides (active_window_seconds / dormant_interval_seconds),
# matching repository.set_agent_loop's server-side bounds exactly so a value
# accepted there can never be rejected/clamped here.
MIN_ACTIVE_WINDOW_S = 300
MAX_ACTIVE_WINDOW_S = 14400
MIN_DORMANT_INTERVAL_S = 600
MAX_DORMANT_INTERVAL_S = 86400

# Phase 5 (plan §6): opencode balance/credit/quota exhaustion alert. Matched
# case-insensitively against the (name, message) OpencodeAdapter.last_error()
# returns. Deliberately broad (payment/402 included) since providers phrase
# this differently -- a false positive just pauses+alerts a bit early, a
# false negative leaves a worker silently stuck, which is worse.
BALANCE_ALERT_RE = re.compile(r"insufficient|balance|credit|quota|payment|402", re.IGNORECASE)
BALANCE_ALERT_COOLDOWN_S = 3600
BALANCE_ALERT_PAUSE_MINUTES = 120


def resolve_t_max(value: int | None) -> int:
    """Validate/apply the --t-max default. Refuses anything below the verify
    ceiling + margin: a lower value would kill an in-flight verify_run."""
    if value is None:
        return DEFAULT_T_MAX_S
    if value < MIN_T_MAX_S:
        raise ValueError(
            f"--t-max {value}s is below the minimum {MIN_T_MAX_S}s "
            f"(verify ceiling {VERIFY_CEILING_S}s + {T_MAX_MARGIN_S}s margin); "
            "refusing, this would kill in-flight verify_run calls."
        )
    return value


# --------------------------------------------------------------------------- #
# TICK RESULT parsing — pure function, unit-tested directly.
# --------------------------------------------------------------------------- #

@dataclass
class TickResult:
    valid: bool                 # a well-formed marker was found
    worked_ids: list[int] = field(default_factory=list)
    ready_to_clear: bool = False
    no_work: bool = False
    reason: str | None = None
    raw: str = ""                # the matched segment, for logging/debugging
    protocol_violation: bool = False  # well-formed but semantically broken
                                       # (e.g. WORKED with no ids) -- MINOR 9c


_MARKER_RE = re.compile(r"tick result:", re.IGNORECASE)
_ID_RE = re.compile(r"#(\d+)")
_REASON_RE = re.compile(r"\(([^)]*)\)")

# MINOR (opus re-review, follow-up to MAJOR 8): READY-TO-CLEAR is matched
# case-SENSITIVE (no re.IGNORECASE) and only against the literal uppercase
# token -- MAJOR 8 widened this to a whole-text, case-INsensitive scan,
# which turned out to also match ordinary lowercase prose that happens to
# contain the phrase (e.g. "we're near the ready-to-clear point"), falsely
# triggering a context clear. Requiring the uppercase token keeps the
# whole-text tolerance (own line, same line, anywhere) MAJOR 8 wanted,
# without matching narrative text. `[-\s]?` tolerates the token being
# written with hyphens, spaces, or run together.
_READY_TO_CLEAR_RE = re.compile(r"READY[-\s]?TO[-\s]?CLEAR")

# Phase 3 note (was a TODO here): the token-budget FORCED_CLEAR backstop does
# NOT hook into this text-scanning parser -- it is driven entirely by the
# side-car's own usage accounting (TokenAccountant.exhausted(), fed by
# Adapter.get_usage()), independent of anything the worker's reply says. A
# worker that ignores the CONTEXT BUDGET line and never emits READY-TO-CLEAR
# is exactly the case this backstop exists for, so tying it to reply-text
# scanning would defeat the purpose. See Sidecar._maybe_forced_clear.


def parse_tick_result(text: str | None) -> TickResult:
    """Tolerant parser: finds the LAST case-insensitive `TICK RESULT:` in
    `text` (the last completed assistant message) and interprets the rest of
    that line. Never raises; a missing/garbled marker just comes back
    invalid=False so the caller can count it as a protocol violation."""
    if not text:
        return TickResult(valid=False, raw="")

    matches = list(_MARKER_RE.finditer(text))
    if not matches:
        return TickResult(valid=False, raw=text[-300:])

    last = matches[-1]
    line_end = text.find("\n", last.end())
    segment = text[last.end(): line_end if line_end != -1 else len(text)].strip()
    lowered = segment.lower()

    # MAJOR 8: READY-TO-CLEAR is searched across the WHOLE reply, not just the
    # marker line -- models sometimes put it on its own line rather than
    # appending it to the TICK RESULT line as the contract asks. Tolerant on
    # purpose; the contract (tick-contract.md) still asks for same-line.
    # Case-SENSITIVE uppercase-token match only -- see _READY_TO_CLEAR_RE.
    # (FORCED_CLEAR is deliberately NOT text-driven: it fires from
    # TokenAccountant.exhausted() in Sidecar._maybe_forced_clear, the backstop
    # for a worker that never emits this token at all.)
    ready_to_clear = bool(_READY_TO_CLEAR_RE.search(text))

    if re.search(r"\bno work\b", lowered):
        reason_match = _REASON_RE.search(segment)
        reason = reason_match.group(1).strip() if reason_match else None
        return TickResult(valid=True, no_work=True, reason=reason, raw=segment)

    ids = [int(m) for m in _ID_RE.findall(segment)]
    if ids:
        return TickResult(valid=True, worked_ids=ids, ready_to_clear=ready_to_clear, raw=segment)

    if "worked" in lowered:
        # MINOR 9c: WORKED with no ids is well-formed enough to parse but
        # violates the contract (ids are mandatory after WORKED). Treat as no
        # work done AND flag it so the caller logs a protocol violation --
        # it must NOT reset the active window as a real WORKED would.
        return TickResult(valid=True, no_work=True, protocol_violation=True,
                           reason="WORKED with no ids (protocol violation)", raw=segment)

    # Marker present but neither WORKED nor NO WORK recognizable -> garbled.
    return TickResult(valid=False, raw=segment)


def build_tick_prompt(worker_prompt: str, tick_contract: str, extra_context: str = "") -> str:
    """Assemble the text injected for one tick: worker prompt + tick contract
    + an optional extra block (Phase 3: the token-budget line, see
    Sidecar._extra_context)."""
    parts = [worker_prompt.rstrip(), tick_contract.rstrip()]
    if extra_context:
        parts.append(extra_context.rstrip())
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Phase 3: token accounting (plan §6). The side-car -- not the worker -- owns
# usage accounting and embeds a budget line into each tick prompt.
# --------------------------------------------------------------------------- #

class TokenAccountant:
    """Tracks context-window usage and cumulative cost for one worker across
    its lifetime of opencode sessions.

    Context usage is inherently PER-SESSION (a fresh session starts at 0
    tokens), so it resets on `reset_session()` -- called by the side-car
    after any clear, drain or forced. Cost is cumulative across the whole
    worker's lifetime, so it is NOT reset: `reset_session()` folds the
    just-ended session's cost into a running baseline before zeroing the
    per-session counter, so `total_cost` never goes backwards across a
    clear.
    """

    def __init__(self, context_limit_tokens: int = 180_000, clear_threshold_pct: int = 70,
                 low_budget_pct: int = 90, margin_pct: int = 15):
        self.context_limit_tokens = context_limit_tokens
        self.clear_threshold_pct = clear_threshold_pct
        self.low_budget_pct = low_budget_pct
        self.margin_pct = margin_pct

        self.context_tokens: int | None = None   # last known, CURRENT session
        self._session_cost: float = 0.0           # last-seen cumulative cost, CURRENT session
        self._cost_baseline: float = 0.0          # summed final cost of PRIOR sessions

    @property
    def total_cost(self) -> float:
        return self._cost_baseline + self._session_cost

    def update(self, usage: dict | None) -> None:
        """Ingest one get_usage() reading. Tolerant of None/malformed input
        -- never raises, and a bad reading simply leaves prior state alone
        rather than resetting it to zero/unknown."""
        if not isinstance(usage, dict):
            return
        tokens = usage.get("context_tokens")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
            self.context_tokens = int(tokens)
        cost = usage.get("session_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            # QA fix (MAJOR): a raw assignment let a stale/smaller cost
            # reading regress `_session_cost` downward; reset_session() would
            # then permanently fold that too-low value into the baseline,
            # silently undercounting cumulative cost forever after. Cost is
            # cumulative-within-a-session by construction (the opencode API
            # never reports it decreasing), so take the max of what we've
            # seen -- a genuine decrease is by definition a bad/stale read,
            # never a real one.
            self._session_cost = max(self._session_cost, float(cost))

    def context_pct(self) -> float | None:
        if self.context_tokens is None or not self.context_limit_tokens:
            return None
        return (self.context_tokens / self.context_limit_tokens) * 100.0

    def should_request_clear(self) -> bool:
        pct = self.context_pct()
        return pct is not None and pct >= self.clear_threshold_pct

    def exhausted(self) -> bool:
        pct = self.context_pct()
        return pct is not None and pct >= self.low_budget_pct

    def budget_line(self) -> str:
        """The CONTEXT BUDGET line embedded in every tick prompt (does NOT
        include the "nearly full" appendix -- Sidecar._extra_context adds
        that separately, gated on should_request_clear())."""
        pct = self.context_pct()
        if pct is None:
            return (
                "CONTEXT BUDGET: unknown (usage unavailable this tick). Be conservative: "
                "prefer small issues you can finish within one tick, memory_write a handoff "
                "summary after each issue, and end with READY-TO-CLEAR once you have."
            )
        return (
            f"CONTEXT BUDGET: ~{self.context_tokens} of {self.context_limit_tokens} tokens "
            f"used ({pct:.0f}%). Apply a {self.margin_pct}% safety margin -- do not start an "
            "issue you cannot finish within the remainder; if too little remains, reply "
            "TICK RESULT: NO WORK (insufficient tokens)."
        )

    def reset_session(self) -> None:
        """Called by the side-car after ANY clear (drain or forced): context
        resets to unknown (the new session starts fresh and hasn't reported
        usage yet), but cost is cumulative across the worker's lifetime --
        fold the just-ended session's cost into the baseline first."""
        self._cost_baseline += self._session_cost
        self._session_cost = 0.0
        self.context_tokens = None


# --------------------------------------------------------------------------- #
# Runtime adapter interface
# --------------------------------------------------------------------------- #

class Adapter:
    """Shape every runtime adapter must implement. opencode (HTTP) is the only
    Phase-2 implementation; tmux adapters (claude/codex) land in Phase 5."""

    def ensure_worker(self) -> None:
        raise NotImplementedError

    def is_idle(self) -> bool:
        raise NotImplementedError

    def inject(self, text: str) -> None:
        raise NotImplementedError

    def read_result(self) -> str | None:
        raise NotImplementedError

    def completion_marker(self) -> str | None:
        """BLOCKER 1 (gate review): an identifier of the newest COMPLETED
        assistant message/turn (opencode: that message's id; FakeAdapter: a
        counter). The side-car snapshots this at inject time and a tick is
        only considered complete once the CURRENT marker differs from that
        baseline AND is_idle() is also true -- is_idle() alone is not
        trustworthy right after an async inject (the worker may not have
        picked up the turn yet, in which case idle is a stale read of the
        *previous* turn, not a fresh completion).

        None means "unreadable this call" (HTTP hiccup, or genuinely no
        completed message yet) -- the caller must treat that as NOT complete
        and fall back to the t_max watchdog as the backstop."""
        return None

    def clear(self) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError

    def get_usage(self) -> dict | None:
        """Phase 3: best-effort token/cost usage for the CURRENT session, or
        None if unavailable/unsupported. Shape: {"context_tokens": int,
        "session_cost": float}. Must NEVER raise -- an adapter that can't
        determine usage (transient error, runtime doesn't expose it) returns
        None and the side-car falls back to the conservative unknown-budget
        line. Default: unsupported."""
        return None

    def current_session_id(self) -> str | None:
        """Optional: an identifier for the current underlying session/
        context, purely for log correlation across a clear() (so operators
        can confirm from the log that a clear actually rotated the session).
        None if the adapter doesn't track one."""
        return None

    def alive(self) -> bool:
        raise NotImplementedError

    def owned_process_dead(self) -> bool:
        """BLOCKER 3: True only if this adapter spawned and OWNS a worker
        subprocess AND that process has confirmably exited (poll() is not
        None). This bypasses the alive()-flap debounce entirely: an owned,
        confirmed-dead process needs no consecutive-failure confirmation.
        Adapters that don't own a process (or can't tell) return False and
        fall back to the alive()-flap counter."""
        return False

    def last_output_change(self) -> float | None:
        """Monotonic timestamp of the last observed output change, for
        t_stuck detection. None means "adapter can't tell" — the watchdog
        then relies on t_max alone (see spec: skip t_stuck, don't guess)."""
        return None

    def last_error(self) -> tuple[str, str] | None:
        """Phase 5 (plan §6): (name, message) of the last runtime-reported
        error, for the side-car's balance/credit-exhaustion alert
        (Sidecar._check_balance_alert). None if the adapter doesn't track
        errors, or there is none. Default: unsupported (tmux adapters have no
        machine-readable error surface; only OpencodeAdapter implements
        this)."""
        return None

    def shutdown(self, kill_worker: bool) -> None:
        """Called once on clean side-car exit. Only tears the worker down if
        kill_worker is True; otherwise the durable session is left alive."""
        return None


# --------------------------------------------------------------------------- #
# OpencodeAdapter — HTTP against `opencode serve` (see oc-api-cheatsheet.md)
# --------------------------------------------------------------------------- #

class OpencodeAdapter(Adapter):
    def __init__(self, base_url: str, directory: str | None = None,
                 provider_id: str | None = None, model_id: str | None = None,
                 timeout: float = 8.0, logger=None,
                 project: str | None = None, agent_id: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.provider_id = provider_id
        self.model_id = model_id
        self.timeout = timeout
        self.project = project
        self.agent_id = agent_id
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._logger = logger or (lambda event, **kv: None)

    # -- MAJOR 4: this adapter owns a single, exactly-titled session; it must
    # never adopt an arbitrary "newest in directory" session (that's the
    # session-hijack bug -- attaching to someone else's in-flight session).
    def _session_title(self) -> str:
        return f"sidecar/{self.project}/agent-{self.agent_id}"

    # -- low-level HTTP -----------------------------------------------------
    def _request(self, method: str, path: str, payload=None, timeout=None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            body = resp.read()
            if not body:
                return None
            return json.loads(body)

    def _reachable(self) -> bool:
        try:
            self._request("GET", "/session/status", timeout=3)
            return True
        except Exception:
            return False

    # -- Adapter interface ----------------------------------------------------
    def ensure_worker(self) -> None:
        if self._reachable():
            self._ensure_session()
            return
        if not self.directory:
            raise RuntimeError(
                "opencode server unreachable at %s and no --opencode-dir given to spawn one"
                % self.base_url
            )
        parsed = urllib.parse.urlparse(self.base_url)
        port = parsed.port or 4096
        self._logger("SPAWN", port=port, directory=self.directory)
        self._proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            cwd=self.directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._reachable():
                self._ensure_session()
                return
            if self._proc.poll() is not None:
                raise RuntimeError("opencode serve exited during startup")
            time.sleep(0.5)
        raise RuntimeError("opencode serve did not become reachable within 60s")

    def _ensure_session(self) -> None:
        if self.session_id:
            return
        title = self._session_title()
        sessions = []
        if self.directory:
            try:
                q = urllib.parse.quote(self.directory, safe="")
                sessions = self._request("GET", f"/session?directory={q}") or []
            except Exception:
                sessions = []
        # MAJOR 4: re-discover ONLY by exact title match (newest such) --
        # never adopt an arbitrary newest session in the directory, that is
        # the session-hijack bug the gate review flagged.
        matches = [s for s in sessions if s.get("title") == title]
        if matches:
            matches.sort(key=lambda s: (s.get("time") or {}).get("updated", 0), reverse=True)
            self.session_id = matches[0]["id"]
        else:
            created = self._request("POST", "/session", {"title": title})
            self.session_id = created["id"]
        self._verify_session_ownership()

    def _verify_session_ownership(self) -> None:
        """QA fix (finding 3): the sidecar's port-selection formula spreads
        projects across different port bands to avoid collision, but that's
        a COLLISION-AVOIDANCE heuristic, not a guarantee -- two fleets on
        one host, a stale process left listening on a recycled port, or a
        simple misconfiguration could still put us in front of an
        `opencode serve` bound to someone ELSE's checkout. Confirm the
        session we just created/attached to is actually scoped to
        self.directory; raise loudly rather than silently operating in the
        wrong directory (which could mean claiming/editing/committing work
        against the wrong project entirely).

        Primary check: GET /session/{id} exposes `directory` directly (per
        the opencode openapi spec) -- compare it to self.directory. Some
        server versions may not expose that field; fall back to confirming
        our session id appears in GET /session?directory=<ours> (the same
        endpoint _ensure_session already uses for title re-discovery).

        Deliberately best-effort on TRANSIENT failures (a network hiccup on
        the verification call itself must not crash a session that may be
        perfectly fine) -- only a POSITIVE mismatch (a real, differing
        directory value, or a real listing that doesn't contain our id)
        raises."""
        if not self.directory:
            return
        try:
            session = self._request("GET", f"/session/{self.session_id}")
        except Exception as exc:
            self._logger("OWNERSHIP_CHECK_ERROR", error=str(exc))
            session = None

        if isinstance(session, dict) and "directory" in session:
            directory = session.get("directory")
            if directory != self.directory:
                raise RuntimeError(
                    "opencode server on this port serves a different directory "
                    f"(expected {self.directory!r}, session {self.session_id} reports {directory!r})"
                )
            return

        # `directory` not exposed on this server's Session object -- fall
        # back to the directory-scoped listing containing our id.
        try:
            q = urllib.parse.quote(self.directory, safe="")
            sessions = self._request("GET", f"/session?directory={q}") or []
        except Exception as exc:
            self._logger("OWNERSHIP_CHECK_ERROR", error=str(exc))
            return
        ids = {s.get("id") for s in sessions if isinstance(s, dict)}
        if self.session_id not in ids:
            raise RuntimeError(
                "opencode server on this port serves a different directory "
                f"(session {self.session_id} not found under directory {self.directory!r})"
            )

    def is_idle(self) -> bool:
        try:
            status = self._request("GET", "/session/status") or {}
        except Exception:
            return False
        if self.session_id is None:
            return True
        return self.session_id not in status

    def inject(self, text: str) -> None:
        payload: dict = {"parts": [{"type": "text", "text": text}]}
        if self.provider_id and self.model_id:
            payload["model"] = {"providerID": self.provider_id, "modelID": self.model_id}
        self._request("POST", f"/session/{self.session_id}/prompt_async", payload)

    def _last_completed_assistant_message(self) -> dict | None:
        try:
            messages = self._request("GET", f"/session/{self.session_id}/message") or []
        except Exception:
            return None
        for msg in reversed(messages):
            info = msg.get("info", {})
            if info.get("role") == "assistant" and (info.get("time") or {}).get("completed"):
                return msg
        return None

    def completion_marker(self) -> str | None:
        # BLOCKER 1: the id of the newest COMPLETED assistant message is the
        # freshness baseline -- see Adapter.completion_marker docstring.
        msg = self._last_completed_assistant_message()
        if msg is None:
            return None
        return (msg.get("info") or {}).get("id")

    def read_result(self) -> str | None:
        msg = self._last_completed_assistant_message()
        if msg is None:
            return None
        parts = msg.get("parts", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def last_error(self) -> tuple[str, str] | None:
        # Phase 5 (plan §6): the last completed assistant message's
        # info.error -- opencode sets this when the provider call itself
        # failed (rate limit, insufficient balance, invalid key, ...) rather
        # than producing a normal reply. Tolerant of any missing/malformed
        # shape -- this feeds an alert path, it must never raise.
        try:
            msg = self._last_completed_assistant_message()
        except Exception:
            return None
        if msg is None:
            return None
        err = (msg.get("info") or {}).get("error")
        if not isinstance(err, dict):
            return None
        name = str(err.get("name") or err.get("type") or "error")
        data = err.get("data")
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or "")
        if not message:
            message = str(err.get("message") or "")
        return (name, message)

    def clear(self) -> None:
        # Cheap and safe: create a NEW session (same owned title -- MAJOR 4),
        # leave the old one in place.
        created = self._request("POST", "/session", {"title": self._session_title()})
        self.session_id = created["id"]

    def current_session_id(self) -> str | None:
        return self.session_id

    @staticmethod
    def _nonneg_int(value) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 0
        return n if n >= 0 else 0

    @classmethod
    def _context_tokens_from(cls, tokens) -> int | None:
        """Current-context occupancy from ONE assistant turn's info.tokens.
        Prefer the server's own per-message `total` when it's a sane positive
        int (it already accounts for every sub-field); otherwise sum ALL parts
        ourselves -- input + output + reasoning + cache.read + cache.write
        (dropping none, the Phase-3 QA fix). Returns None when tokens is
        absent/garbled so get_usage can degrade to the unknown-budget line
        rather than report a bogus 0."""
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        raw_total = tokens.get("total")
        if isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool) and raw_total > 0:
            return int(raw_total)
        return (cls._nonneg_int(tokens.get("input"))
                + cls._nonneg_int(tokens.get("output"))
                + cls._nonneg_int(tokens.get("reasoning"))
                + cls._nonneg_int(cache.get("read"))
                + cls._nonneg_int(cache.get("write")))

    def get_usage(self) -> dict | None:
        # DEFECT-SIDECAR-2 (plan §14): context occupancy is a CURRENT-turn
        # quantity, NOT a session sum. GET /session exposes Session.tokens as a
        # per-session CUMULATIVE running total that only ever grows; feeding it
        # to TokenAccountant (which divides by the ~180k window) reported 1589%
        # after a single work cycle and tripped a spurious FORCED_CLEAR (first
        # soak, 2026-07-23). The live context window is the LAST completed
        # assistant message's OWN per-turn tokens, so read those. session_cost
        # is genuinely cumulative, so it still comes from GET /session.
        # Tolerant of any missing/malformed field -- polled every tick, a hiccup
        # must degrade to "usage unknown", never crash the side-car.
        if not self.session_id:
            return None
        msg = self._last_completed_assistant_message()
        if msg is None:
            return None
        info = msg.get("info")
        info = info if isinstance(info, dict) else {}
        context_tokens = self._context_tokens_from(info.get("tokens"))
        if context_tokens is None:
            return None

        # Cumulative session cost, best-effort: a failure here degrades cost to
        # 0.0 but must never suppress the (already-obtained) token reading.
        session_cost = 0.0
        if self.session_id:
            try:
                data = self._request("GET", f"/session/{self.session_id}")
                if isinstance(data, dict):
                    try:
                        session_cost = float(data.get("cost"))
                    except (TypeError, ValueError):
                        session_cost = 0.0
            except Exception:
                session_cost = 0.0
        if session_cost < 0:
            session_cost = 0.0

        return {"context_tokens": context_tokens, "session_cost": session_cost}

    def restart(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        elif self.session_id:
            try:
                self._request("POST", f"/session/{self.session_id}/abort")
            except Exception:
                pass
        self.session_id = None
        self.ensure_worker()

    def alive(self) -> bool:
        if self._proc is not None and self._proc.poll() is not None:
            return False
        return self._reachable()

    def owned_process_dead(self) -> bool:
        # BLOCKER 3: only meaningful when we spawned the process ourselves.
        return self._proc is not None and self._proc.poll() is not None

    def last_output_change(self) -> float | None:
        # Phase 2: not tracked (would need message-stream timestamp diffing).
        # Watchdog falls back to t_max alone, as the spec allows.
        return None

    def shutdown(self, kill_worker: bool) -> None:
        if not kill_worker:
            return
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# TmuxAdapter — claude/codex TUIs driven via tmux capture-pane/send-keys
# (Phase 5, plan §5 + §12). ALL tmux interaction goes through the injectable
# `tmux_runner` seam (self._tmux) so tests never shell out to a real tmux.
# --------------------------------------------------------------------------- #

_TICK_MARKER_LINE_RE = re.compile(r"tick result:", re.IGNORECASE)

# QA fix (finding 4): paste-buffer/load-buffer names are sanitized to tmux/
# shell-safe characters so a project key with e.g. slashes or spaces can't
# produce an invalid (or, worse, a DIFFERENT project's) buffer name.
_BUFFER_PROJECT_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9-]")

# A dead TUI drops the pane back to an interactive shell -- these are the
# shells started/wrapped by every runtime we launch (bash/zsh directly, sh/dash
# as a POSIX fallback). pane_current_command reporting one of these means the
# TUI process exited, not that it is merely "idle at a prompt".
_TMUX_SHELL_COMMANDS = frozenset({"bash", "zsh", "sh", "dash"})


def _default_tmux_runner(args: list[str]) -> subprocess.CompletedProcess:
    """The real subprocess seam: `args` is the full argv including the `tmux`
    binary name itself (e.g. ["tmux", "capture-pane", "-p", "-t", target]).
    10s timeout: every tmux call here is a local IPC round-trip to the tmux
    server, never something that legitimately blocks for a worker-scale
    duration."""
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


class TmuxAdapter(Adapter):
    """Drives a claude/codex TUI living in one tmux pane. Fragility
    mitigations (plan §12, accepted risk): inject() NEVER send-keys the raw
    prompt text (load-buffer/paste-buffer only -- send-keys is reserved for
    short, literal control input: Enter and the `/clear` slash command,
    which the TUI must receive as typed keystrokes to recognize it as a
    command rather than pasted text). Idle detection is capture-pane hash
    stability, the primary signal the plan accepts as "good enough, bounded
    by completion_marker + coalescing" rather than parsing TUI chrome.

    completion_marker()/get_usage() intentionally return simple, monotonic,
    heuristic values (a marker-line count+hash tuple; a chars/4 token
    estimate) -- see each method's docstring. Both are conservative by
    design: the goal is "never worse than the t_max watchdog backstop",
    not TUI introspection precision.
    """

    def __init__(self, *, tmux_target: str, spawn_cmd: str | None = None,
                 project: str | None = None, agent_id: int | None = None,
                 idle_quiet_seconds: int = 10, capture_lines: int = 2000,
                 tail_lines: int = 40, result_lines: int = 60,
                 tmux_runner=None, clock=time.monotonic, logger=None):
        self.tmux_target = tmux_target
        self.spawn_cmd = spawn_cmd
        self.project = project
        self.agent_id = agent_id
        self.idle_quiet_seconds = idle_quiet_seconds
        self.capture_lines = capture_lines
        self.tail_lines = tail_lines
        self.result_lines = result_lines
        self._tmux = tmux_runner or _default_tmux_runner
        self.clock = clock
        self._logger = logger or (lambda event, **kv: None)

        # -- is_idle()/last_output_change() stability tracking ------------
        self._last_hash: str | None = None
        self._stable_since: float | None = None

        # -- get_usage() heuristic (spec: chars injected + chars read, /4,
        # monotonic within a session, reset on clear()/restart()) ---------
        self._chars_total = 0

        # -- completion_marker() adapter-LOCAL monotonic state (QA fix,
        # finding 1): capture-pane's `-S -N` scrollback window can TRUNCATE
        # older lines as the pane fills up, which would make a naive
        # (count, hash-of-last-line) marker regress (count drops) even
        # though nothing about the actual latest completion changed. See
        # completion_marker()'s docstring for the full reasoning.
        self._marker_last_count = 0
        self._marker_last_hash: str | None = None
        self._marker_counter = 0

    # -- low-level tmux seam --------------------------------------------------
    def _tmux_run(self, *args: str) -> subprocess.CompletedProcess:
        return self._tmux(["tmux", *args])

    def _buffer_name(self) -> str:
        # QA fix (finding 4): a bare "sidecar-<agent_id>" buffer name
        # collides across PROJECTS sharing this host whenever two sidecars
        # for the SAME agent_id number (common -- e.g. every project's
        # backend-dev-worker is agent 1) inject at literally the same tmux
        # instant, one paste-buffer clobbering the other's in-flight
        # load/paste. Namespacing by project (sanitized to tmux/shell-safe
        # chars) makes the name unique per (project, agent) pair.
        project = _BUFFER_PROJECT_SANITIZE_RE.sub("", self.project) if self.project else ""
        project = project or "x"
        agent = self.agent_id if self.agent_id is not None else "x"
        return f"sidecar-{project}-{agent}"

    def _capture(self, lines: int) -> str:
        res = self._tmux_run("capture-pane", "-p", "-t", self.tmux_target, "-S", f"-{lines}")
        if res.returncode != 0:
            raise RuntimeError(f"tmux capture-pane failed: {(res.stderr or '').strip()}")
        return res.stdout or ""

    def _reset_tracking_state(self) -> None:
        """Called on clear()/restart(): a fresh session/pane means the
        idle-stability window, the usage estimate, AND the completion-marker
        monotonic state (finding 1) all start over."""
        self._last_hash = None
        self._stable_since = None
        self._chars_total = 0
        self._marker_last_count = 0
        self._marker_last_hash = None
        self._marker_counter = 0

    def _respawn(self) -> subprocess.CompletedProcess:
        # QA fix (finding 6): respawn-pane's target pane runs whatever its
        # DEFAULT shell is (the login shell tmux started it with, e.g. zsh
        # on macOS) -- NOT necessarily bash. spawn_cmd itself may be a
        # composite shell command line (e.g. finding 2's `env K=V ... claude
        # ...`, or a chain with &&/pipes), which only a REAL shell can parse
        # correctly; handing it to the pane's default shell risked subtly
        # wrong parsing (or outright failure) depending on what that shell
        # happens to be. Explicit `bash -lc <spawn_cmd>` as THREE separate
        # argv items (not one pre-quoted string) makes tmux exec bash
        # directly with spawn_cmd as its single -c argument -- no additional
        # shell-quoting/escaping needed here, tmux passes each argv item
        # through untouched.
        return self._tmux_run("respawn-pane", "-k", "-t", self.tmux_target,
                               "bash", "-lc", self.spawn_cmd)

    # -- Adapter interface ------------------------------------------------
    def ensure_worker(self) -> None:
        if self.alive():
            return
        if not self.spawn_cmd:
            raise RuntimeError(
                f"tmux pane {self.tmux_target} has no live TUI and no spawn_cmd "
                "was given to (re)spawn it"
            )
        # respawn-pane -k force-kills whatever's currently in the pane (a bare
        # shell, harmlessly, or nothing at all) and execs spawn_cmd -- this
        # covers both "dead TUI, shell left behind" and "pane exists but never
        # had a TUI started in it" with one call.
        res = self._respawn()
        if res.returncode != 0:
            raise RuntimeError(f"tmux respawn-pane failed: {(res.stderr or '').strip()}")
        self._reset_tracking_state()

    def is_idle(self) -> bool:
        now = self.clock()
        try:
            text = self._capture(self.tail_lines)
        except Exception:
            return False
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        if digest != self._last_hash:
            self._last_hash = digest
            self._stable_since = now
            return False
        if self._stable_since is None:
            self._stable_since = now
            return False
        return (now - self._stable_since) >= self.idle_quiet_seconds

    def inject(self, text: str) -> None:
        # Never send-keys the raw prompt (plan §12): write it to a temp file,
        # load it into a named tmux paste buffer, paste (and delete) that
        # buffer into the pane, then send a bare Enter to submit it. Claude
        # and codex TUIs both accept this as bracketed-paste input.
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp_path = tmp.name
        try:
            tmp.write(text)
            tmp.close()
            buf = self._buffer_name()
            res = self._tmux_run("load-buffer", "-b", buf, tmp_path)
            if res.returncode != 0:
                raise RuntimeError(f"tmux load-buffer failed: {(res.stderr or '').strip()}")
            res = self._tmux_run("paste-buffer", "-d", "-b", buf, "-t", self.tmux_target)
            if res.returncode != 0:
                raise RuntimeError(f"tmux paste-buffer failed: {(res.stderr or '').strip()}")
            res = self._tmux_run("send-keys", "-t", self.tmux_target, "Enter")
            if res.returncode != 0:
                raise RuntimeError(f"tmux send-keys (Enter) failed: {(res.stderr or '').strip()}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        self._chars_total += len(text)

    def completion_marker(self) -> str:
        """QA fix (finding 1): adapter-LOCAL monotonic counter, not a raw
        (count, hash) snapshot. `capture-pane -S -N` is a SCROLLBACK WINDOW,
        not the whole session history -- as the pane fills up, older marker
        lines fall off the front of that window, which would make a naive
        "count of TICK RESULT lines currently visible" REGRESS even though
        nothing about the actual latest completion changed. The Sidecar's
        baseline-equality check (`marker == self.tick_baseline`) has no
        notion of "this went backwards, ignore it" -- a regressed marker
        risks either a stuck "already seen this" false match or, worse,
        corrupting the comparison. Fix: keep (last_seen_count,
        last_seen_hash, monotonic_counter) on the adapter itself; the
        returned value only ever counts up.

        Per call: capture, compute the current count of "TICK RESULT:"
        lines and a hash of the LAST such line plus up to 4 lines
        FOLLOWING it (not just the marker line alone -- two ticks that
        both emit byte-identical marker text, e.g. consecutive "TICK
        RESULT: NO WORK", are disambiguated by whatever differs in the
        trailing context; and even if that window hash also happens to
        collide, a genuine new occurrence still raises the raw COUNT, which
        alone is sufficient to register a change). Then:
          - hash CHANGED or count INCREASED -> bump the counter, adopt the
            new (count, hash) as the reference.
          - count DECREASED with the hash UNCHANGED -> scrollback
            truncation (the newest marker + its trailing window are
            identical to what we already saw; some OLDER marker just
            scrolled out of the captured window) -- a complete no-op,
            counter/reference left exactly as they were so a later real
            increase is still measured against the true (higher) peak.
          - anything else unchanged -> no-op (nothing new).

        Always returns a string (never None) -- raises on capture failure
        only, same contract as before (BLOCKER 1: the Sidecar maps that to
        _BASELINE_UNKNOWN and recovers on the next successful read, exactly
        like an opencode HTTP hiccup)."""
        text = self._capture(self.capture_lines)
        lines = text.splitlines()
        marker_idxs = [i for i, ln in enumerate(lines) if _TICK_MARKER_LINE_RE.search(ln)]
        count = len(marker_idxs)
        if count:
            last_idx = marker_idxs[-1]
            window = lines[last_idx:last_idx + 5]   # marker line + up to 4 following
            current_hash = hashlib.sha1(
                "\n".join(window).encode("utf-8", errors="replace")
            ).hexdigest()
        else:
            current_hash = None

        count_increased = count > self._marker_last_count
        count_decreased = count < self._marker_last_count
        hash_changed = current_hash != self._marker_last_hash
        truncated = count_decreased and not hash_changed

        if not truncated:
            if hash_changed or count_increased:
                self._marker_counter += 1
            self._marker_last_count = count
            self._marker_last_hash = current_hash
        # else: truncation -- leave counter/last_count/last_hash untouched.

        return str(self._marker_counter)

    def read_result(self) -> str | None:
        try:
            text = self._capture(self.capture_lines)
        except Exception:
            return None
        lines = text.splitlines()
        tail = "\n".join(lines[-self.result_lines:]) if lines else ""
        if not tail:
            return None
        # Spec (get_usage heuristic): count chars of each read_result() return
        # once per tick -- this is that one call per completed tick.
        self._chars_total += len(tail)
        return tail

    def clear(self) -> None:
        # Slash commands must be TYPED, not pasted (plan implementation
        # notes): send-keys "/clear" + Enter as two literal keystroke groups,
        # never via load-buffer/paste-buffer.
        res = self._tmux_run("send-keys", "-t", self.tmux_target, "/clear", "Enter")
        if res.returncode != 0:
            raise RuntimeError(f"tmux send-keys (/clear) failed: {(res.stderr or '').strip()}")
        self._reset_tracking_state()

    def restart(self) -> None:
        if not self.spawn_cmd:
            raise RuntimeError("TmuxAdapter.restart() requires spawn_cmd (none configured)")
        res = self._respawn()
        if res.returncode != 0:
            raise RuntimeError(f"tmux respawn-pane failed: {(res.stderr or '').strip()}")
        self._reset_tracking_state()

    def get_usage(self) -> dict | None:
        # Heuristic only (spec): no TUI introspection, so this is a rough,
        # conservative, MONOTONIC-within-a-session estimate -- (chars
        # injected + chars read via read_result()) / 4 as a token count.
        # session_cost is always 0.0 (claude/codex billing isn't observable
        # here; get_usage() docs already allow session_cost to be a stub).
        return {"context_tokens": self._chars_total // 4, "session_cost": 0.0}

    def _expected_command(self) -> str | None:
        """basename of the first whitespace-separated token of spawn_cmd
        (e.g. "claude --foo" -> "claude", "/usr/bin/bash script.sh" ->
        "bash"). None if spawn_cmd isn't configured -- alive() then falls
        back to the plain shell-means-dead heuristic, unchanged."""
        if not self.spawn_cmd:
            return None
        first = self.spawn_cmd.strip().split(None, 1)
        if not first:
            return None
        return os.path.basename(first[0])

    def alive(self) -> bool:
        res = self._tmux_run("display-message", "-p", "-t", self.tmux_target,
                              "#{pane_current_command}")
        if res.returncode != 0:
            return False
        cmd = (res.stdout or "").strip()
        if not cmd:
            return False
        # BUGFIX (live tmux e2e, coordinator report): the bare-shell-means-
        # dead heuristic below only makes sense when we EXPECT the pane to
        # be running a non-shell TUI (claude/codex/node/...). When spawn_cmd
        # itself IS (or is launched via) a shell -- e.g. a bash-driven
        # fake-TUI/wrapper script used for e2e/testing -- tmux reports
        # pane_current_command == "bash" for the ENTIRE lifetime of a
        # perfectly healthy pane. Treating that as "dead" made alive()
        # permanently False for such a worker, which drove an endless
        # 3-strike restart loop (observed live: 66 ALIVE_PROBE_FAIL / 33
        # RESTART / 0 TICK_RESULT in 100s, despite the pane visibly having
        # completed and printed its result). Fix: if the command we EXPECT
        # to see is itself a shell, or the observed command matches the
        # expected one exactly, any non-empty pane_current_command counts
        # as alive -- we cannot distinguish "shell running our script" from
        # "shell after the script exited" via this field alone, so we don't
        # try. The shell-means-dead heuristic still applies (unchanged) when
        # we expect a real TUI (spawn_cmd starts with a non-shell binary,
        # e.g. claude/codex) and don't see it -- that case is unambiguous:
        # the TUI dropped back to a shell, so it's dead.
        expected = self._expected_command()
        if expected is not None and (expected in _TMUX_SHELL_COMMANDS or cmd == expected):
            return True
        return cmd not in _TMUX_SHELL_COMMANDS

    def last_output_change(self) -> float | None:
        return self._stable_since

    def shutdown(self, kill_worker: bool) -> None:
        if not kill_worker:
            return
        try:
            self._tmux_run("kill-pane", "-t", self.tmux_target)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Dashboard client (heartbeat + pause/loop policy poll)
# --------------------------------------------------------------------------- #

class DashboardClient:
    def __init__(self, base_url: str, agent_id: int, project: str,
                 timeout: float = 8.0, logger=None):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.project = project
        self.timeout = timeout
        self._logger = logger or (lambda event, **kv: None)

    def heartbeat(self, status: str | None = None) -> bool:
        q = urllib.parse.quote(self.project, safe="")
        url = f"{self.base_url}/agents/{self.agent_id}/heartbeat?project={q}"
        if status is not None:
            # Phase 4 (plan §7): self-report working|idle|dormant so the
            # dashboard/engine's own status column (and the staleness view)
            # reflect the side-car's real state, not just "last touched".
            # An invalid value is the dashboard's problem to ignore (it
            # never 4xxs a heartbeat) -- the side-car always sends one of
            # its own three known states, never arbitrary text.
            url += f"&status={urllib.parse.quote(status, safe='')}"
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
            return True
        except Exception as exc:
            self._logger("HEARTBEAT_FAIL", error=str(exc))
            return False

    def get_policy(self) -> dict | None:
        q = urllib.parse.quote(self.project, safe="")
        url = f"{self.base_url}/agents/{self.agent_id}/pause?project={q}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            self._logger("STATE_POLL", ok=False, error=str(exc))
            return None

    def pause(self, minutes: int = BALANCE_ALERT_PAUSE_MINUTES) -> bool:
        """Phase 5 (plan §6): POST the EXISTING human/engine pause endpoint
        (POST /agents/pause, form-encoded) so the engine stops assigning this
        agent new work -- used by the balance-alert path. Best-effort: the
        caller (Sidecar._check_balance_alert) treats a failure as non-fatal."""
        q = urllib.parse.quote(self.project, safe="")
        url = f"{self.base_url}/agents/pause?project={q}"
        data = urllib.parse.urlencode(
            {"agent_id": self.agent_id, "minutes": minutes}
        ).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
            return True
        except Exception as exc:
            self._logger("PAUSE_POST_FAIL", error=str(exc))
            return False

    def alert(self, subject: str, body: str = "") -> bool:
        """Phase 5 (plan §6/§C): POST the NEW /alerts endpoint so a human sees
        the balance/credit exhaustion in Correspondence (ADR-ORCH-006:
        failures surface to Correspondence, never silently). Best-effort,
        same as pause() above."""
        q = urllib.parse.quote(self.project, safe="")
        url = f"{self.base_url}/alerts?project={q}"
        data = urllib.parse.urlencode(
            {"agent_id": self.agent_id, "subject": subject, "body": body}
        ).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
            return True
        except Exception as exc:
            self._logger("ALERT_POST_FAIL", error=str(exc))
            return False


# --------------------------------------------------------------------------- #
# Side-car state machine
# --------------------------------------------------------------------------- #

DEFAULT_POLICY = {"pause_seconds": 0, "loop_enabled": True, "poll_interval_seconds": 300}

# BLOCKER 1 / MAJOR 2 (opus re-review): sentinel for "we tried to snapshot
# the completion marker at inject time and it raised" -- distinct from a
# legitimate `None` return (which means "adapter read fine, there's just no
# completed message yet"). Conflating the two by storing `None` for both
# reopens the stale-read bug: a later, successful-but-late marker read of
# the SAME pre-existing stale message would then compare as "different from
# None" and get wrongly collected. See Sidecar._maybe_collect_result.
_BASELINE_UNKNOWN = object()


def _coerce_bool(value, default: bool) -> bool:
    """Tolerant bool coercion for policy fields that may arrive as real
    JSON booleans, stringly-typed booleans, or garbage. Never raises."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int_field(value, *, min_v: int | None = None, max_v: int | None = None) -> int | None:
    """Tolerant int coercion for policy fields. Accepts a real `int`, or a
    `float` that is integral (e.g. `600.0` -> `600`) -- MINOR (opus
    re-review): a dashboard that serializes cadence fields as JSON floats
    must not have those silently rejected as "garbage" (a float
    pause_seconds of 600.0 falling back to the 0/unpaused default would be
    a silent, dangerous un-pause). Rejects `bool` explicitly (bool is an
    `int` subclass in Python, and True/False must never become 1/0 here),
    non-integral floats, and out-of-[min_v, max_v] values. Returns None on
    any rejection; the caller substitutes its own documented default."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float) and value.is_integer():
        coerced = int(value)
    else:
        return None
    if min_v is not None and coerced < min_v:
        return None
    if max_v is not None and coerced > max_v:
        return None
    return coerced


def _coerce_policy(raw, last_good: dict) -> dict:
    """MAJOR 6 (gate review): validate/clamp a policy dict fetched from the
    dashboard. Malformed input (wrong types, out-of-range values, missing
    keys, or `raw` not even being a dict) must NEVER raise and must NEVER
    produce a negative or zero cadence. Per-field, a bad value falls back to
    the documented safe default (not necessarily `last_good`); if anything
    unexpected blows up during coercion, the entire last-known-good policy
    is kept as the outermost safety net."""
    try:
        if not isinstance(raw, dict):
            return dict(last_good)

        poll = _coerce_int_field(raw.get("poll_interval_seconds"),
                                  min_v=MIN_POLL_INTERVAL_S, max_v=MAX_POLL_INTERVAL_S)
        if poll is None:
            poll = DEFAULT_POLICY["poll_interval_seconds"]

        pause = _coerce_int_field(raw.get("pause_seconds"), min_v=0)
        if pause is None:
            pause = DEFAULT_POLICY["pause_seconds"]

        loop_enabled = _coerce_bool(raw.get("loop_enabled"), default=DEFAULT_POLICY["loop_enabled"])

        return {
            "poll_interval_seconds": poll,
            "pause_seconds": pause,
            "loop_enabled": loop_enabled,
        }
    except Exception:
        return dict(last_good)


class Sidecar:
    """Single-threaded cadence/watchdog state machine. `run()` drives it off
    the wall clock (or an injected clock/sleeper for tests); `step(now)` is
    the whole state machine for one iteration and is what tests call
    directly so they never sleep real time."""

    def __init__(self, *, adapter: Adapter, dashboard, worker_prompt: str, tick_contract: str,
                 active_window: int = 1800, dormant_interval: int = 3600,
                 heartbeat_interval: int = 20, state_poll_interval: int = 45,
                 t_stuck: int | None = 900, t_max: int = DEFAULT_T_MAX_S,
                 restart_cooldown: int = DEFAULT_RESTART_COOLDOWN_S,
                 kill_worker_on_exit: bool = False,
                 context_limit_tokens: int = 180_000, context_clear_pct: int = 70,
                 context_low_pct: int = 90, budget_margin_pct: int = 15,
                 token_accountant: "TokenAccountant | None" = None,
                 clock=time.monotonic, sleeper=time.sleep, tick_resolution: float = 1.0,
                 log_file: str | None = None):
        self.adapter = adapter
        self.dashboard = dashboard
        self.worker_prompt = worker_prompt
        self.tick_contract = tick_contract
        self.active_window = active_window
        self.dormant_interval = dormant_interval
        self.heartbeat_interval = heartbeat_interval
        self.state_poll_interval = state_poll_interval
        self.t_stuck = t_stuck
        self.t_max = t_max
        self.restart_cooldown = restart_cooldown
        self.kill_worker_on_exit = kill_worker_on_exit
        # Phase 3: accepts a pre-built accountant (tests that want to inspect
        # it directly) or builds one from the individual CLI-shaped kwargs.
        self.accountant = token_accountant or TokenAccountant(
            context_limit_tokens=context_limit_tokens,
            clear_threshold_pct=context_clear_pct,
            low_budget_pct=context_low_pct,
            margin_pct=budget_margin_pct,
        )
        self.clock = clock
        self.sleeper = sleeper
        self.tick_resolution = tick_resolution

        now = self.clock()
        self.state = "ACTIVE"
        self.last_worked_at = now
        self.next_tick_at = now             # fire the first tick immediately
        self.pending = False
        self.tick_start_at: float | None = None
        # BLOCKER 1 / MAJOR 2: completion-marker baseline snapshotted at
        # inject time. One of: a real marker value (str), `None` (adapter
        # read fine, no completed message exists yet -- a legitimate
        # baseline), or `_BASELINE_UNKNOWN` (the snapshot itself raised --
        # see _maybe_collect_result for how this is recovered).
        self.tick_baseline = None
        self.protocol_violations = 0
        self.restart_times: list[float] = []
        self._consecutive_alive_failures = 0    # BLOCKER 3
        # Coordinator e2e report (live tmux): nothing bounded restart
        # frequency, so a persistent alive()-false (or a crash-looping TUI)
        # restarted every ~3 probes -- seconds apart for a real claude/codex
        # session, each respawn replaying the initial prompt (token burn +
        # session churn). None means "never restarted yet" -- the very first
        # restart is never suppressed. See _restart().
        self._last_restart_at: float | None = None
        self.policy = dict(DEFAULT_POLICY)
        # Force both the heartbeat and the state-poll to fire on the very
        # first step() rather than waiting a full interval.
        self.last_heartbeat_at = now - heartbeat_interval - 1
        self.last_state_poll_at = now - state_poll_interval - 1
        # Phase 4 (plan §7): last wake_at this process has observed, for
        # check_wake's dedup-on-increase. None means "never observed yet" --
        # the very first observation only sets this baseline, it never fires.
        self._last_wake_at: datetime | None = None
        # Plan §15: last orchestrator work-signal (last_work_at) this process has
        # observed, for check_work_signal's dedup-on-increase. Same baseline rule
        # as _last_wake_at — first observation sets it without firing.
        self._last_work_at: datetime | None = None
        self._stop = False
        self._log_fh = open(log_file, "a") if log_file else None
        # Phase 5 (plan §6): last time (self.clock() units) a BALANCE_ALERT
        # fired -- None means "never fired yet". Rate-limits the
        # pause+alert POSTs to at most once per BALANCE_ALERT_COOLDOWN_S.
        self._last_balance_alert_at: float | None = None
        # QA fix (finding 7b): idle-wait fallback bookkeeping -- how long a
        # DUE tick has been undeliverable purely because is_idle() won't
        # settle, and the completion_marker() value observed when that wait
        # began (to confirm nothing has actually changed in the meantime).
        # None means "not currently waiting". See _maybe_deliver_tick.
        self._coalesce_wait_since: float | None = None
        self._coalesce_wait_marker = None

    # -- logging --------------------------------------------------------------
    def _log(self, event: str, **kv) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        kvstr = " ".join(f"{k}={v}" for k, v in kv.items())
        line = f"ts={ts} state={self.state} event={event}"
        if kvstr:
            line += f" {kvstr}"
        print(line, flush=True)
        if self._log_fh:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()

    def _current_session_id_safe(self) -> str | None:
        # Phase 3: best-effort, for log correlation across a clear() -- an
        # adapter's current_session_id() must never be allowed to break
        # logging if it misbehaves.
        try:
            return self.adapter.current_session_id()
        except Exception:
            return None

    # -- Phase-4 wake relay (plan §7, migration 0024) --------------------------
    def check_wake(self, state_json: dict) -> None:
        """Relay the orchestrator's per-project wake_at into an immediate tick,
        deduped on increase. `state_json` is the same /agents/{id}/pause
        payload _maybe_poll_state already fetches every state_poll_interval;
        `wake_at` is an ISO timestamp (or absent/null before anyone has ever
        bumped it for this project).

        Dedup rule: fire ONLY when wake_at is strictly greater than the last
        value THIS process observed.
          - First observation (self._last_wake_at is None): establish the
            baseline WITHOUT firing. Without this, every side-car (re)start
            would treat whatever wake_at already happens to exist as a brand
            new wake signal and immediately tick -- a false wake on every
            restart, not just on an actual promotion event.
          - Equal to the last observed value: no-op. A dashboard/network
            hiccup that returns the same payload twice must never re-fire.
          - Strictly greater: fires exactly once (bumping the baseline before
            returning, so the next equal-or-lower read is inert).

        On fire: go ACTIVE (a wake always means "there's new work, stop being
        dormant") with the active window reset (last_worked_at = now, as if a
        tick had just completed), and mark a tick pending + due immediately.
        This deliberately does NOT call _inject_tick directly -- that would
        bypass the tick_start_at in-flight guard living in _maybe_deliver_tick
        (this method runs from _maybe_poll_state, earlier in the SAME step,
        so a real tick could already be in flight). Setting pending+due lets
        the tick actually fire through the normal _maybe_deliver_tick ->
        _inject_tick path later in this same step (or the next one, if a tick
        is still in flight) -- suppression (pause/loop_enabled) and the
        in-flight guard are honored exactly as for any other trigger."""
        if not isinstance(state_json, dict):
            return
        raw = state_json.get("wake_at")
        if not raw:
            return
        try:
            wake_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return
        if self._last_wake_at is None:
            self._last_wake_at = wake_at
            return
        if wake_at <= self._last_wake_at:
            return
        self._last_wake_at = wake_at
        self.state = "ACTIVE"
        self.last_worked_at = self.clock()
        self.pending = True
        self.next_tick_at = self.clock()
        self._log("WAKE", wake_at=wake_at.isoformat())

    def check_work_signal(self, state_json: dict) -> None:
        """Plan §15: orchestrator-authoritative work signal. `last_work_at` (ISO
        ts or null) is the newest work event the orchestrator recorded for THIS
        agent (report_work / code_committed / tests_run on its issues) — the
        authoritative "did the worker do work", independent of whether the worker
        emitted a TICK RESULT marker in its reply. When it advances since the last
        poll the worker demonstrably did real work, so:
          - reset the active window (last_worked_at = now) → no false
            `window_elapsed` dormancy for a worker that IS producing,
          - wake from dormant → resume normal cadence,
          - clear the protocol-violation counter → a marker-shy but working model
            (opencode/codex) never trips the 'stuck-suspect' alert or gets
            watchdog-restarted for "no TICK RESULT".
        It deliberately does NOT force an extra tick (unlike a wake) — normal
        cadence proceeds; this only keeps the window/liveness bookkeeping honest.

        Dedup mirrors check_wake: first observation sets the baseline WITHOUT
        firing (else every restart mis-reads pre-existing work as new); equal =
        no-op; strictly greater = fire once (baseline bumped before returning)."""
        if not isinstance(state_json, dict):
            return
        raw = state_json.get("last_work_at")
        if not raw:
            return
        try:
            work_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return
        if self._last_work_at is None:
            self._last_work_at = work_at
            return
        if work_at <= self._last_work_at:
            return
        self._last_work_at = work_at
        self.last_worked_at = self.clock()
        self.protocol_violations = 0
        if self.state == "DORMANT":
            self.state = "ACTIVE"
            self._log("ACTIVE", reason="orch_work_signal")
        self._log("WORK_SIGNAL", last_work_at=work_at.isoformat())

    # -- signal handling --------------------------------------------------------
    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        self._stop = True

    def _shutdown(self) -> None:
        self.adapter.shutdown(self.kill_worker_on_exit)
        self._log("SHUTDOWN", kill_worker_on_exit=self.kill_worker_on_exit)
        if self._log_fh:
            self._log_fh.close()

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        self.install_signal_handlers()
        self._ensure_worker_safe()
        try:
            while not self._stop:
                self.step(self.clock())
                self.sleeper(self.tick_resolution)
        finally:
            self._shutdown()

    def _ensure_worker_safe(self) -> None:
        # BLOCKER 2: a startup hiccup must not crash the process -- the
        # watchdog's restart path (which itself calls ensure_worker again
        # via adapter.restart()) is the retry mechanism from here on.
        try:
            self.adapter.ensure_worker()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("ENSURE_WORKER_ERROR", error=str(exc))

    # -- one iteration of the state machine ---------------------------------
    def step(self, now: float) -> None:
        # BLOCKER 2: the whole iteration is guarded -- a transient HTTP
        # error (or any other unexpected exception) anywhere in one step
        # must never be fatal to the run loop. Only Ctrl-C / SystemExit
        # propagate, everything else is logged as STEP_ERROR and the loop
        # continues on the next iteration.
        try:
            self._maybe_heartbeat(now)
            self._maybe_poll_state(now)
            # MINOR 9a: harvest a completed result BEFORE the watchdog can
            # see it as "still running" and kill it -- a tick that finished
            # at t_max+epsilon must be collected, not restarted away.
            self._maybe_collect_result(now)
            self._check_watchdog(now)
            self._check_window(now)
            # Phase 3: the exhaustion backstop runs BEFORE tick delivery so a
            # forced clear always lands before the next tick is injected,
            # never mid-tick (it is itself gated on no tick being in flight).
            self._maybe_forced_clear(now)
            self._maybe_deliver_tick(now)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("STEP_ERROR", error=str(exc))

    def _cadence(self) -> int:
        if self.state == "ACTIVE":
            return int(self.policy.get("poll_interval_seconds") or DEFAULT_POLICY["poll_interval_seconds"])
        return self.dormant_interval

    # -- heartbeat / state poll (always on, independent of ticking) ---------
    def _current_status(self) -> str:
        """Phase 4 (plan §7): the side-car's own liveness self-report, sent
        on every heartbeat. A tick in flight always means 'working' even if
        the window has technically elapsed (the window check only ever runs
        between ticks -- see _check_window); otherwise it's the state-machine
        state verbatim ('idle' for ACTIVE-but-nothing-in-flight, 'dormant')."""
        if self.tick_start_at is not None:
            return "working"
        return "dormant" if self.state == "DORMANT" else "idle"

    def _maybe_heartbeat(self, now: float) -> None:
        if now - self.last_heartbeat_at < self.heartbeat_interval:
            return
        self.last_heartbeat_at = now
        ok = self.dashboard.heartbeat(status=self._current_status())
        if not ok:
            self._log("HEARTBEAT_FAIL")

    def _maybe_poll_state(self, now: float) -> None:
        if now - self.last_state_poll_at < self.state_poll_interval:
            return
        self.last_state_poll_at = now
        raw_policy = self.dashboard.get_policy()
        if raw_policy is None:
            # A dashboard blip must not stop ticking: keep the last-good policy.
            self._log("STATE_POLL", ok=False, cached_pause=self.policy.get("pause_seconds"),
                      cached_loop_enabled=self.policy.get("loop_enabled"))
            return
        # MAJOR 6: never trust the raw payload verbatim -- clamp/validate
        # every field, falling back to safe defaults (or the last-good
        # policy wholesale, if coercion itself errors) rather than letting
        # garbage produce a negative or zero cadence.
        self.policy = _coerce_policy(raw_policy, self.policy)
        self._log("STATE_POLL", ok=True, pause_seconds=self.policy.get("pause_seconds"),
                  loop_enabled=self.policy.get("loop_enabled"),
                  poll_interval_seconds=self.policy.get("poll_interval_seconds"))
        # Phase 4: cadence-window overrides (active_window_seconds /
        # dormant_interval_seconds) from the dashboard, when present and
        # valid, take priority over the --active-window/--dormant-interval
        # CLI flags this process started with. Unlike poll_interval_seconds/
        # pause_seconds above, an absent or out-of-bounds value does NOT fall
        # back to some separate hardcoded default -- it simply leaves
        # self.active_window/self.dormant_interval at whatever they already
        # are (the CLI value, until/unless a later poll supplies a valid
        # override). Wrapped defensively: a malformed payload must degrade to
        # "keep the current cadence", never raise out of a state-poll.
        try:
            aw = _coerce_int_field(raw_policy.get("active_window_seconds"),
                                    min_v=MIN_ACTIVE_WINDOW_S, max_v=MAX_ACTIVE_WINDOW_S)
            if aw is not None:
                self.active_window = aw
            di = _coerce_int_field(raw_policy.get("dormant_interval_seconds"),
                                    min_v=MIN_DORMANT_INTERVAL_S, max_v=MAX_DORMANT_INTERVAL_S)
            if di is not None:
                self.dormant_interval = di
        except Exception as exc:
            self._log("CADENCE_OVERRIDE_ERROR", error=str(exc))
        # Phase 4: wake relay -- see check_wake's docstring for the dedup
        # rule. Uses the same raw (pre-coercion) payload since wake_at isn't
        # part of the cadence policy dict.
        self.check_wake(raw_policy)
        # Plan §15: orchestrator-authoritative work signal — reset the active
        # window / wake / clear violations when the orchestrator shows this agent
        # produced work, independent of any TICK RESULT marker. Same raw payload.
        self.check_work_signal(raw_policy)

    # -- watchdog -------------------------------------------------------------
    def _alive_safe(self) -> bool:
        # BLOCKER 2: alive() itself can raise (HTTP hiccup) -- that must
        # count as a probe failure, not a crash.
        try:
            return self.adapter.alive()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("ALIVE_ERROR", error=str(exc))
            return False

    def _check_watchdog(self, now: float) -> None:
        # BLOCKER 3: an owned subprocess that has confirmably exited is
        # certainly dead -- restart immediately, no debounce needed.
        try:
            owned_dead = self.adapter.owned_process_dead()
        except Exception:
            owned_dead = False
        if owned_dead:
            self._restart(now, reason="proc_dead")
            return

        if self._alive_safe():
            self._consecutive_alive_failures = 0
        else:
            self._consecutive_alive_failures += 1
            if self._consecutive_alive_failures >= ALIVE_FAILURE_THRESHOLD:
                self._restart(now, reason="dead")
                return
            # A single flap (or two) must NEVER abort a session -- even one
            # with a tick in flight well within t_max. Log and keep going;
            # the next probe may well succeed.
            self._log("ALIVE_PROBE_FAIL", consecutive=self._consecutive_alive_failures,
                      threshold=ALIVE_FAILURE_THRESHOLD)

        if self.tick_start_at is None:
            return
        elapsed = now - self.tick_start_at
        if elapsed > self.t_max:
            self._restart(now, reason="t_max")
            return
        if self.t_stuck is not None:
            last_change = self.adapter.last_output_change()
            if last_change is not None and (now - last_change) > self.t_stuck:
                self._restart(now, reason="t_stuck")
                return

    def _restart(self, now: float, reason: str) -> None:
        # Single choke point (mirrors MAJOR 5's _inject_tick pattern): EVERY
        # restart call site -- the 3-strike alive()-failure path, t_max,
        # t_stuck, AND the owned-process-dead fast path -- goes through this
        # method, so the cooldown applies uniformly (the fast path is a
        # debounce bypass for CONFIRMING death quickly, not an exemption from
        # rate-limiting the resulting restart). Counters that led here
        # (_consecutive_alive_failures, tick_start_at/tick_baseline) are
        # deliberately left untouched on suppression -- the watchdog will
        # keep calling _restart() every subsequent step while the underlying
        # condition persists, and it fires for real the moment the cooldown
        # lapses, rather than the failure being silently dropped.
        if (self.restart_cooldown > 0 and self._last_restart_at is not None
                and (now - self._last_restart_at) < self.restart_cooldown):
            self._log("RESTART_SUPPRESSED", reason=reason,
                      cooldown_remaining=round(self.restart_cooldown - (now - self._last_restart_at), 1))
            return
        self._last_restart_at = now
        self.adapter.restart()
        # BLOCKER (opus re-review): the in-flight tick (if any) is abandoned
        # by definition once we restart -- clear both fields BEFORE the
        # trailing _inject_tick below. If this isn't cleared and that
        # trailing inject hits the suppressed branch (pause / loop_enabled
        # false), tick_start_at keeps its STALE pre-restart value, so the
        # t_max watchdog check re-fires on every subsequent step -> a
        # restart storm (~1/step) for the whole duration of the pause. The
        # trailing _inject_tick re-arms both fields only if it actually
        # injects.
        self.tick_start_at = None
        self.tick_baseline = None
        self._consecutive_alive_failures = 0
        self.restart_times = [t for t in self.restart_times if now - t <= 3600]
        self.restart_times.append(now)
        count_1h = len(self.restart_times)
        self._log("RESTART", reason=reason, restarts_1h=count_1h)
        if count_1h >= 3:
            # Phase 2: a loud log is the alert; engine-side alerting is Phase 4/5.
            print(f"ALERT: side-car restarted {count_1h} times in the last hour "
                  f"(latest reason={reason})", file=sys.stderr, flush=True)
        self._inject_tick(now, reason="post-restart")

    # -- window / dormancy -----------------------------------------------------
    def _check_window(self, now: float) -> None:
        if self.state == "ACTIVE" and (now - self.last_worked_at) > self.active_window:
            self.state = "DORMANT"
            self._log("DORMANT", reason="window_elapsed")
            # MINOR 9b: recompute next_tick_at unconditionally, even with a
            # tick in flight -- previously this was skipped whenever
            # tick_start_at was not None, which could leave the DORMANT
            # cadence stale.
            self.next_tick_at = now + self.dormant_interval

    # -- Phase 3: proactive exhaustion backstop --------------------------------
    def _maybe_forced_clear(self, now: float) -> None:
        """Backstop for a worker that ignores the CONTEXT NEARLY FULL
        instruction and never emits READY-TO-CLEAR: once the side-car's own
        usage accounting says the context is exhausted, clear proactively --
        but only when it is safe to (worker idle, no tick in flight, not
        paused/loop-disabled) and NEVER mid-tick. This runs every step, but
        is self-limiting: reset_session() sets context_tokens back to
        unknown, so exhausted() reads False again until the next tick's
        get_usage() reports fresh (low) usage -- no repeated clearing."""
        if self.tick_start_at is not None:
            return
        if self._suppressed():
            return
        if not self.accountant.exhausted():
            return
        if not self.adapter.is_idle():
            return
        try:
            self.adapter.clear()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("CLEAR_ERROR", error=str(exc), forced=True)
            return
        self.accountant.reset_session()
        self._log("FORCED_CLEAR", reason="context", session_id=self._current_session_id_safe())

    # -- tick injection ---------------------------------------------------------
    def _extra_context(self) -> str:
        # Phase 3: the side-car-measured CONTEXT BUDGET line, plus a
        # "nearly full" appendix instructing the worker to checkpoint and
        # emit READY-TO-CLEAR once should_request_clear() trips.
        line = self.accountant.budget_line()
        if self.accountant.should_request_clear():
            line += (
                "\n\nCONTEXT NEARLY FULL: finish/checkpoint the current issue, memory_write "
                "a handoff summary, and end this tick with READY-TO-CLEAR."
            )
        return line

    def _build_prompt(self) -> str:
        return build_tick_prompt(self.worker_prompt, self.tick_contract, self._extra_context())

    # MAJOR 5: single choke point for suppression. Every path that wants to
    # inject a tick -- scheduled/coalesced delivery, the drain handshake
    # after READY-TO-CLEAR, and the post-restart re-inject -- goes through
    # _inject_tick, so none of them can bypass pause/loop_enabled.
    def _suppressed(self) -> bool:
        return (self.policy.get("pause_seconds", 0) or 0) > 0 or not self.policy.get("loop_enabled", True)

    def _inject_tick(self, now: float, reason: str) -> None:
        if self._suppressed():
            self.pending = True
            self._log("SUPPRESSED", reason=reason, pause_seconds=self.policy.get("pause_seconds"),
                      loop_enabled=self.policy.get("loop_enabled"))
            return
        # BLOCKER 2: inject() can raise (transient HTTP error). A failed
        # inject must leave `pending` True so the next step() retries it --
        # it must NOT be silently dropped.
        try:
            self.adapter.inject(self._build_prompt())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self.pending = True
            self._log("INJECT_ERROR", reason=reason, error=str(exc))
            self._check_balance_alert(now)
            return
        self.tick_start_at = now
        # BLOCKER 1: snapshot the completion-marker baseline at the moment
        # of injection -- this is what makes is_idle() trustworthy later.
        # MAJOR 2 (opus re-review): a raise here must NOT collapse to `None`
        # -- `None` is a legitimate baseline (adapter read fine, no
        # completed message exists yet) and treating "the read itself
        # failed" the same way reopens the stale-read bug: a later,
        # successful-but-late read of the SAME pre-existing stale message
        # would then look "different from None" and get wrongly collected.
        # `_BASELINE_UNKNOWN` is recovered in _maybe_collect_result.
        try:
            self.tick_baseline = self.adapter.completion_marker()
        except Exception:
            self.tick_baseline = _BASELINE_UNKNOWN
        self.pending = False
        self._log("TICK_INJECT", reason=reason, session_id=self._current_session_id_safe())

    def _maybe_deliver_tick(self, now: float) -> None:
        """Note this is NOT gated on tick_start_at for BECOMING due: a tick
        can be "due" for the next cadence slot while the PREVIOUS tick is
        still running (a 35-55min verify tick vs a 5min cadence is the
        normal case, not an edge case). Becoming due just raises the
        `pending` flag exactly once; repeated due events while busy/in-
        flight/suppressed are absorbed for free because the flag is already
        set. `pending` alone (not next_tick_at) gates delivery, so a
        suppressed-then-resumed or busy-then-idle tick fires the moment
        it's actually deliverable, instead of waiting for the next cadence
        slot. Suppression itself is handled inside _inject_tick (MAJOR 5),
        not here."""
        became_due = False
        if not self.pending and now >= self.next_tick_at:
            self.pending = True
            self.next_tick_at = now + self._cadence()
            became_due = True

        if not self.pending:
            self._coalesce_wait_since = None
            return

        # BLOCKER 1: never start a new tick while one is already in flight,
        # even if the adapter's is_idle() looks true (that can be a stale
        # read of the *previous* turn right after an async inject).
        if self.tick_start_at is not None:
            self._coalesce_wait_since = None
            return

        if not self.adapter.is_idle():
            # QA fix (finding 7b, animated-TUI hash instability): bound the
            # damage from a tail hash that never settles (spinner, blinking
            # cursor, live status line) even though the worker is genuinely
            # at rest -- there is NO tick in flight here (guarded above), so
            # the only thing keeping this tick from being delivered is
            # is_idle() itself. If it's been stuck for a while AND the
            # completion marker hasn't moved AT ALL in that time (nothing
            # new is actually happening -- a genuinely busy worker would
            # have a stale/unchanging marker too, but so would a merely
            # flickering idle prompt, which is exactly the ambiguous case
            # this is meant to catch), inject anyway rather than coalescing
            # forever. Adapters with no idle_quiet_seconds (opencode:
            # is_idle() is an instantaneous HTTP fact, not a hash-stability
            # heuristic) never get this fallback -- there's no hash flakiness
            # to bound damage from. Heuristic thresholds (3x) -- tune against
            # the real-TUI soak.
            idle_quiet = getattr(self.adapter, "idle_quiet_seconds", None)
            if idle_quiet:
                try:
                    current_marker = self.adapter.completion_marker()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    current_marker = _BASELINE_UNKNOWN
                if (self._coalesce_wait_since is None
                        or current_marker != self._coalesce_wait_marker):
                    self._coalesce_wait_since = now
                    self._coalesce_wait_marker = current_marker
                elif (now - self._coalesce_wait_since) > 3 * idle_quiet:
                    self._log("IDLE_FALLBACK", waited=round(now - self._coalesce_wait_since, 1))
                    self._coalesce_wait_since = None
                    self._inject_tick(now, reason="idle_fallback")
                    return
            if became_due:
                self._log("COALESCE")
            return

        self._coalesce_wait_since = None
        self._inject_tick(now, reason="scheduled" if became_due else "coalesced")

    # -- result collection --------------------------------------------------
    def _maybe_collect_result(self, now: float) -> None:
        if self.tick_start_at is None:
            return
        # QA fix (finding 7a, animated-TUI hash instability): the collection
        # gate is now completion-MARKER stability, not whole-tail is_idle().
        # is_idle() remains the gate for INJECTION only (_maybe_deliver_tick)
        # -- an animated/streaming TUI (spinner, live-updating status line)
        # can keep the whole-tail hash from EVER settling even after the
        # model has actually finished and printed its TICK RESULT marker;
        # gating collection on is_idle() starved collection entirely in
        # that case (the coordinator's live e2e: "0 TICK_RESULT despite the
        # pane visibly having completed"). The ORIGINAL protection this
        # is_idle() check provided (the "false-idle window right after an
        # async inject" BLOCKER 1 concern below) was never actually is_idle()
        # itself -- it's the marker-vs-baseline comparison that follows,
        # which is unaffected by removing this gate.
        #
        # BLOCKER 1: is_idle() alone was never sufficient on its own -- right
        # after an async inject, the worker may not have started the turn
        # yet, so a raw idle reading can be stale (against the *previous*
        # completed message). Only treat the tick as complete once the
        # completion marker has actually moved past the injection baseline.
        # A bare marker-unchanged observation must NEVER clear tick_start_at
        # -- the t_max watchdog remains the backstop for a worker that never
        # reports a fresh completion.

        if self.tick_baseline is _BASELINE_UNKNOWN:
            # MAJOR 2 (opus re-review): the snapshot at inject time raised,
            # so we have no trustworthy reference point to compare against
            # -- we cannot tell "already completed" from "hasn't started"
            # apart. Retry the snapshot now (while still idle, i.e. while
            # the turn may not have started yet) and, if it succeeds,
            # PROMOTE that reading to the baseline rather than treating it
            # as a fresh completion. This is deliberately conservative: it
            # never collects on the same step a baseline is (re)established,
            # so the one true edge case it doesn't handle -- the tick
            # genuinely finishing in the exact window the baseline was
            # unknown -- is simply deferred, not lost: the next step's
            # marker read will differ from the just-promoted baseline and
            # collect normally, and the t_max watchdog is the backstop if a
            # real completion is somehow never observed to differ.
            try:
                marker = self.adapter.completion_marker()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._log("MARKER_ERROR", error=str(exc))
                return
            self.tick_baseline = marker
            self._log("BASELINE_RECOVERED", marker=marker)
            return

        try:
            marker = self.adapter.completion_marker()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("MARKER_ERROR", error=str(exc))
            return
        if marker is None or marker == self.tick_baseline:
            return

        # QA fix (finding 7a): the marker differs from baseline -- a change
        # is visible -- but require it to be STABLE across two consecutive
        # reads before trusting it, in place of the whole-tail is_idle()
        # gate removed above. A mid-render/animated read could otherwise
        # catch a not-yet-fully-written marker line. A second read that
        # disagrees means it's still changing -- treat that exactly like
        # "not yet observed a stable completion" (return; tick_baseline is
        # untouched, so the ORIGINAL baseline is still what the next step
        # compares against, and the next step gets its own fresh 2-read
        # confirmation).
        try:
            marker_confirm = self.adapter.completion_marker()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("MARKER_ERROR", error=str(exc))
            return
        if marker_confirm != marker:
            return

        self._collect_result(now)

    def _collect_result(self, now: float) -> None:
        text = self.adapter.read_result()
        self._update_usage()
        # Phase 5 (plan §6): checked on EVERY result collection, independent
        # of whether the tick parsed as WORKED/NO WORK/garbled -- a balance
        # exhaustion can arrive instead of a normal reply at all.
        self._check_balance_alert(now)
        result = parse_tick_result(text)
        self.tick_start_at = None
        self.tick_baseline = None

        if not result.valid:
            self.protocol_violations += 1
            self._log("TICK_RESULT", valid=False, violations=self.protocol_violations,
                      raw=result.raw[:200])
            if self.protocol_violations >= 3:
                print(f"ALERT: {self.protocol_violations} consecutive ticks without a "
                      "TICK RESULT marker — stuck-suspect", file=sys.stderr, flush=True)
            return

        if result.protocol_violation:
            # MINOR 9c: e.g. "TICK RESULT: WORKED" with no ids -- well-formed
            # enough to parse, but breaks the contract. Counts as a protocol
            # violation and must NOT reset the active window (no_work is
            # already the parse outcome, so last_worked_at/state are
            # untouched, same as a real NO WORK).
            self.protocol_violations += 1
            self._log("TICK_RESULT", worked=False, no_work=True, reason=result.reason,
                      protocol_violation=True, violations=self.protocol_violations)
            if self.protocol_violations >= 3:
                print(f"ALERT: {self.protocol_violations} consecutive ticks without a "
                      "TICK RESULT marker — stuck-suspect", file=sys.stderr, flush=True)
            return

        self.protocol_violations = 0

        if result.no_work:
            self._log("TICK_RESULT", worked=False, no_work=True, reason=result.reason)
            return

        self.last_worked_at = now
        if self.state == "DORMANT":
            self.state = "ACTIVE"
            self._log("ACTIVE", reason="worked")
        self._log("TICK_RESULT", worked=True, ids=result.worked_ids,
                  ready_to_clear=result.ready_to_clear)

        if result.ready_to_clear:
            # BLOCKER 2: clear() can raise. If it does, log and skip the
            # immediate re-tick -- do NOT crash, and do NOT retry the clear
            # itself; normal cadence resumes and picks up the next tick.
            try:
                self.adapter.clear()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._log("CLEAR_ERROR", error=str(exc))
                return
            self.accountant.reset_session()
            self._log("CLEAR", session_id=self._current_session_id_safe())
            # QA fix (finding 5): don't call _inject_tick directly here --
            # that bypassed the is_idle() gate entirely, which is fine for
            # opencode (clear() creates a brand-new session that reads idle
            # immediately) but WRONG for tmux: right after send-keys("/clear",
            # "Enter") the pane is BUSY processing that command, so pasting
            # the next tick's prompt into it immediately risked interleaving
            # with /clear's own output or landing before the TUI is ready
            # for input. Mark the tick pending + due NOW and let it flow
            # through the SAME idle-gated path every other tick uses
            # (_maybe_deliver_tick, called later in this same step()) --
            # opencode still drains same-step (idle immediately after
            # clear()), tmux waits for the TUI to actually settle.
            self.pending = True
            self.next_tick_at = now

    # -- Phase 3: usage accounting --------------------------------------------
    def _update_usage(self) -> None:
        """Read adapter.get_usage() and feed the accountant. Adapter.get_usage()
        is contractually never-raising, but this is wrapped defensively
        anyway (BLOCKER-2 style): a usage hiccup must never break result
        collection."""
        try:
            usage = self.adapter.get_usage()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("USAGE_ERROR", error=str(exc))
            return
        self.accountant.update(usage)
        pct = self.accountant.context_pct()
        self._log("USAGE",
                  context_tokens=self.accountant.context_tokens,
                  pct=("unknown" if pct is None else round(pct, 1)),
                  cost=round(self.accountant.total_cost, 4))

    # -- Phase 5: opencode balance/credit-exhaustion alert (plan §6) ----------
    def _check_balance_alert(self, now: float) -> None:
        """Checked after every result collection AND after a failed inject
        (INJECT_ERROR). adapter.last_error() is opt-in (default None on the
        base Adapter -- tmux adapters have no error surface and this is
        simply a no-op for them). On a balance/credit/quota/payment match:
        log BALANCE_ALERT, pause the agent (engine stops assigning work --
        the SAME /agents/pause the human "cooldown" control uses) and post a
        human-visible Correspondence alert (POST /alerts) so someone reloads
        the account. Deliberately does NOT go dormant-until-reset: unlike
        claude/codex's time-window exhaustion, a cost/balance exhaustion has
        no rollover -- pause + alert-a-human is the only recovery. Both
        POSTs are best-effort (DashboardClient.pause/alert already swallow
        their own failures) and this whole method must never raise -- it
        runs on the hot path of every tick collection."""
        try:
            err = self.adapter.last_error()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("LAST_ERROR_ERROR", error=str(exc))
            return
        if not err:
            return
        name, message = err
        if not BALANCE_ALERT_RE.search(f"{name or ''} {message or ''}"):
            return
        if (self._last_balance_alert_at is not None
                and (now - self._last_balance_alert_at) < BALANCE_ALERT_COOLDOWN_S):
            return
        self._last_balance_alert_at = now
        self._log("BALANCE_ALERT", name=name, message=message[:200])
        try:
            self.dashboard.pause(minutes=BALANCE_ALERT_PAUSE_MINUTES)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("BALANCE_ALERT_PAUSE_ERROR", error=str(exc))
        try:
            self.dashboard.alert(
                subject=(f"Agent {getattr(self.dashboard, 'agent_id', '?')} "
                         f"({getattr(self.dashboard, 'project', '?')}) out of balance/credit"),
                body=(f"Runtime reported: {name}: {message}\n\n"
                      "Reload the DigitalOcean / open-model API account, then clear "
                      "the pause on the Agents page (or wait for it to expire)."),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._log("BALANCE_ALERT_POST_ERROR", error=str(exc))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable-worker side-car")
    p.add_argument("--agent-id", type=int, required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dashboard", required=True, help="dashboard base URL")

    p.add_argument("--runtime", default="opencode", choices=["opencode", "tmux"],
                    help="opencode: HTTP against `opencode serve`. tmux (Phase 5): drives a "
                         "claude/codex TUI living in a tmux pane via capture-pane/send-keys.")
    p.add_argument("--opencode-url", help="base URL of the opencode serve instance")
    p.add_argument("--opencode-dir", help="project directory to spawn `opencode serve` in if unreachable")
    p.add_argument("--opencode-provider-id", help="model.providerID for injected prompts (optional)")
    p.add_argument("--opencode-model-id", help="model.modelID for injected prompts (optional)")

    # Phase 5: tmux runtime (claude/codex TUIs).
    p.add_argument("--tmux-target", help="tmux pane target, e.g. 'session:window.pane', for --runtime tmux")
    p.add_argument("--tmux-spawn-cmd",
                    help="shell command `tmux respawn-pane` uses to (re)launch the TUI if the pane "
                         "is dead/bare-shell -- required for ensure_worker()/restart() to recover")
    p.add_argument("--idle-quiet-seconds", type=int, default=10,
                    help="tmux runtime: seconds capture-pane output must be unchanged to count as idle")

    p.add_argument("--prompt-file", required=True, help="path to the already-rendered worker prompt")
    p.add_argument("--tick-contract", help="path to tick-contract.md (default: prompts/tick-contract.md "
                                            "next to this script)")

    p.add_argument("--active-window", type=int, default=1800)
    p.add_argument("--dormant-interval", type=int, default=3600)
    p.add_argument("--heartbeat", type=int, default=20)
    p.add_argument("--state-poll", type=int, default=45)
    p.add_argument("--t-stuck", type=int, default=900)
    p.add_argument("--t-max", type=int, default=None,
                    help=f"default: max(3600, verify ceiling {VERIFY_CEILING_S} + margin {T_MAX_MARGIN_S})")
    p.add_argument("--restart-cooldown", type=int, default=DEFAULT_RESTART_COOLDOWN_S,
                    help="minimum seconds between watchdog restarts (0 disables the cooldown, "
                         f"restoring immediate/pre-cooldown behavior; default {DEFAULT_RESTART_COOLDOWN_S})")

    p.add_argument("--kill-worker-on-exit", action="store_true")
    p.add_argument("--log-file", help="also append log lines to this file")

    # Phase 3: token accounting / budget-embedded ticks (plan §6).
    p.add_argument("--context-limit-tokens", type=int, default=180_000,
                    help="context-window size the budget line is computed against")
    p.add_argument("--context-clear-pct", type=int, default=70,
                    help="context %% at/above which the tick prompt asks for READY-TO-CLEAR")
    p.add_argument("--context-low-pct", type=int, default=90,
                    help="context %% at/above which the side-car proactively force-clears")
    p.add_argument("--budget-margin-pct", type=int, default=15,
                    help="safety margin the worker is told to reserve before starting new work")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        t_max = resolve_t_max(args.t_max)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable; parser.error exits, keeps type-checkers happy

    if args.runtime == "opencode" and not args.opencode_url:
        parser.error("--opencode-url is required for --runtime opencode")
    if args.runtime == "tmux" and not args.tmux_target:
        parser.error("--tmux-target is required for --runtime tmux")

    if args.restart_cooldown < 0:
        parser.error(f"--restart-cooldown must be >= 0 (got {args.restart_cooldown})")

    if not (0 < args.context_clear_pct < args.context_low_pct <= 100):
        parser.error(
            "--context-clear-pct/--context-low-pct must satisfy "
            f"0 < clear < low <= 100 (got clear={args.context_clear_pct}, "
            f"low={args.context_low_pct})"
        )

    # QA fix (MEDIUM): a zero/negative context-limit-tokens makes
    # context_pct() return None (or a nonsense negative number) forever,
    # which silently disables the entire safety layer (should_request_clear
    # / exhausted() never trip, no budget line ever computed) -- refuse it
    # at parse time instead of letting it degrade quietly at runtime.
    if args.context_limit_tokens <= 0:
        parser.error(
            f"--context-limit-tokens must be a positive integer (got {args.context_limit_tokens})"
        )

    worker_prompt = Path(args.prompt_file).read_text()
    contract_path = (Path(args.tick_contract) if args.tick_contract
                      else Path(__file__).resolve().parent / "prompts" / "tick-contract.md")
    tick_contract = contract_path.read_text()

    if args.runtime == "opencode":
        adapter = OpencodeAdapter(
            base_url=args.opencode_url,
            directory=args.opencode_dir,
            provider_id=args.opencode_provider_id,
            model_id=args.opencode_model_id,
            project=args.project,
            agent_id=args.agent_id,
        )
    else:
        adapter = TmuxAdapter(
            tmux_target=args.tmux_target,
            spawn_cmd=args.tmux_spawn_cmd,
            project=args.project,
            agent_id=args.agent_id,
            idle_quiet_seconds=args.idle_quiet_seconds,
        )
    dashboard = DashboardClient(args.dashboard, args.agent_id, args.project)

    sidecar = Sidecar(
        adapter=adapter,
        dashboard=dashboard,
        worker_prompt=worker_prompt,
        tick_contract=tick_contract,
        active_window=args.active_window,
        dormant_interval=args.dormant_interval,
        heartbeat_interval=args.heartbeat,
        state_poll_interval=args.state_poll,
        t_stuck=args.t_stuck,
        t_max=t_max,
        restart_cooldown=args.restart_cooldown,
        kill_worker_on_exit=args.kill_worker_on_exit,
        context_limit_tokens=args.context_limit_tokens,
        context_clear_pct=args.context_clear_pct,
        context_low_pct=args.context_low_pct,
        budget_margin_pct=args.budget_margin_pct,
        log_file=args.log_file,
    )
    sidecar.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
