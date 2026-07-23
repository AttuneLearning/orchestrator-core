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

Runtime adapters implement `Adapter` (below). Phase 2 ships `OpencodeAdapter`
only; a `FakeAdapter` used for tests lives in tests/test_sidecar.py. tmux
adapters (claude/codex) are Phase 5 — the interface is adapter-shaped already
but no tmux code is written here.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
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

# MAJOR 6: bounds enforced on dashboard-supplied cadence policy.
MIN_POLL_INTERVAL_S = 60
MAX_POLL_INTERVAL_S = 7200


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
    # TODO(phase-3): hook the token-budget FORCED_CLEAR trigger here too, same
    # whole-text scan, once the Phase-3 token-budget spec lands.
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
            return
        created = self._request("POST", "/session", {"title": title})
        self.session_id = created["id"]

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

    def clear(self) -> None:
        # Cheap and safe: create a NEW session (same owned title -- MAJOR 4),
        # leave the old one in place.
        created = self._request("POST", "/session", {"title": self._session_title()})
        self.session_id = created["id"]

    def current_session_id(self) -> str | None:
        return self.session_id

    def get_usage(self) -> dict | None:
        # Phase 3: GET /session/{id} exposes cumulative per-session
        # Session.tokens / Session.cost (oc-api-cheatsheet.md). Tolerant of
        # any missing/malformed field -- this must NEVER raise, it is polled
        # every tick and a hiccup here must degrade to "usage unknown", not
        # crash the side-car.
        if not self.session_id:
            return None
        try:
            data = self._request("GET", f"/session/{self.session_id}")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        def _nonneg_int(value) -> int:
            try:
                n = int(value)
            except (TypeError, ValueError):
                return 0
            return n if n >= 0 else 0

        tokens = data.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}

        # QA fix (MAJOR): the previous version summed only input + cache.read
        # + output, silently dropping cache.write and reasoning tokens -- both
        # of which count against the real context window. Prefer the
        # server's own `total` when it's a sane positive int (it already
        # accounts for every sub-field); only fall back to summing the parts
        # ourselves -- ALL of them -- when `total` is missing/invalid.
        raw_total = tokens.get("total")
        total_int = None
        if isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool) and raw_total > 0:
            total_int = int(raw_total)

        if total_int is not None:
            context_tokens = total_int
        else:
            context_tokens = (_nonneg_int(tokens.get("input"))
                               + _nonneg_int(tokens.get("output"))
                               + _nonneg_int(tokens.get("reasoning"))
                               + _nonneg_int(cache.get("read"))
                               + _nonneg_int(cache.get("write")))

        try:
            session_cost = float(data.get("cost"))
        except (TypeError, ValueError):
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

    def heartbeat(self) -> bool:
        q = urllib.parse.quote(self.project, safe="")
        url = f"{self.base_url}/agents/{self.agent_id}/heartbeat?project={q}"
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
        self.policy = dict(DEFAULT_POLICY)
        # Force both the heartbeat and the state-poll to fire on the very
        # first step() rather than waiting a full interval.
        self.last_heartbeat_at = now - heartbeat_interval - 1
        self.last_state_poll_at = now - state_poll_interval - 1
        self._stop = False
        self._log_fh = open(log_file, "a") if log_file else None

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

    # -- Phase-4 wake-relay hook (no-op placeholder) ---------------------------
    def check_wake(self, state_json: dict) -> None:
        """TODO(phase-4): relay the orchestrator's wake_at into an immediate
        tick trigger, deduped on increase. The dashboard payload the side-car
        already polls doesn't carry wake_at yet, so there's nothing to do
        here until Phase 4 lands the engine-side field."""
        return None

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
    def _maybe_heartbeat(self, now: float) -> None:
        if now - self.last_heartbeat_at < self.heartbeat_interval:
            return
        self.last_heartbeat_at = now
        ok = self.dashboard.heartbeat()
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
            return

        # BLOCKER 1: never start a new tick while one is already in flight,
        # even if the adapter's is_idle() looks true (that can be a stale
        # read of the *previous* turn right after an async inject).
        if self.tick_start_at is not None:
            return

        if not self.adapter.is_idle():
            if became_due:
                self._log("COALESCE")
            return

        self._inject_tick(now, reason="scheduled" if became_due else "coalesced")

    # -- result collection --------------------------------------------------
    def _maybe_collect_result(self, now: float) -> None:
        if self.tick_start_at is None:
            return
        if not self.adapter.is_idle():
            return
        # BLOCKER 1: is_idle() alone is NOT sufficient -- right after an
        # async inject, the worker may not have started the turn yet, so
        # is_idle() can read as true against the *previous* completed
        # message (a stale read). Only treat the tick as complete once the
        # completion marker has actually moved past the injection baseline.
        # A bare idle observation (marker unchanged, or unreadable) must
        # NEVER clear tick_start_at -- the t_max watchdog remains the
        # backstop for a worker that never reports a fresh completion.

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
        self._collect_result(now)

    def _collect_result(self, now: float) -> None:
        text = self.adapter.read_result()
        self._update_usage()
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
            self._inject_tick(now, reason="drain")  # immediate, no cadence wait

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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable-worker side-car")
    p.add_argument("--agent-id", type=int, required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dashboard", required=True, help="dashboard base URL")

    p.add_argument("--runtime", default="opencode", choices=["opencode"],
                    help="tmux runtimes (claude/codex) are Phase 5, not yet implemented")
    p.add_argument("--opencode-url", help="base URL of the opencode serve instance")
    p.add_argument("--opencode-dir", help="project directory to spawn `opencode serve` in if unreachable")
    p.add_argument("--opencode-provider-id", help="model.providerID for injected prompts (optional)")
    p.add_argument("--opencode-model-id", help="model.modelID for injected prompts (optional)")

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

    if args.runtime != "opencode" or not args.opencode_url:
        parser.error("--opencode-url is required for --runtime opencode (the only Phase-2 runtime)")

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

    adapter = OpencodeAdapter(
        base_url=args.opencode_url,
        directory=args.opencode_dir,
        provider_id=args.opencode_provider_id,
        model_id=args.opencode_model_id,
        project=args.project,
        agent_id=args.agent_id,
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
