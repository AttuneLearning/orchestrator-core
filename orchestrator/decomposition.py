"""Decomposition policy — pure, no I/O (like state_machine / pipelines / focus).

Encapsulates the three controls from the decomposition/routing spec
(docs reference: engine-decomposition-routing-fixes.md):

  * decompose_mode  — resolve an explicit override or the simple-goal heuristic
                      into "single" (one implementation issue, no children) or
                      "full" (decompose, still bounded by the engine's caps).
  * is_qa_duplicate — drop candidate sub-issues that merely duplicate a QA gate
                      (standalone test / typecheck / lint / e2e / hmr / bundle-
                      output issues). Verification lives as acceptance criteria
                      on the implementation issue; the QA runner owns the gates.
  * routing_violations — pure invariant check for a decomposed child set.

The engine imports these; all DB access stays in the engine/repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SINGLE = "single"
FULL = "full"


# --------------------------------------------------------------------------- #
# Decomposition tiers — capability-scaled bootstrap presets
#
# A project is bootstrapped at ONE tier, matched to the reliability/bandwidth of
# the fleet's models (set via `setup-project --decomposition-tier` or the
# per-project `decomposition_tier:` setting). The tier scales three things:
#
#   * SIZING           — the granularity clause fed to the reasoner's decompose
#                        prompt (coarse feature-slices → single deliverables →
#                        the smallest safely-shippable step).
#   * internal_parallelism — whether ONE issue may be worked by internally
#                        parallel subagents (only worthwhile for high-capability
#                        owners driving a larger slice; off everywhere else so a
#                        weaker worker keeps a single, auditable focus).
#   * midrun_checks    — whether the engine runs mid-run direction/drift advisories
#                        while an issue is in progress (for low-reliability fleets
#                        that drift; advisory only — never blocks forward progress).
#
# Each tier also carries THRESHOLD OVERRIDES. These are layered UNDER any explicit
# per-project threshold override (explicit config always wins): a bootstrapped
# project with no thresholds set inherits the tier's granularity caps; a project
# that pins its own thresholds keeps them.
# --------------------------------------------------------------------------- #

HIGH = "high"
MID = "mid"
REMEDIAL = "remedial"
DEFAULT_TIER = MID


@dataclass(frozen=True)
class TierProfile:
    name: str
    sizing: str
    internal_parallelism: bool
    midrun_checks: bool
    thresholds: dict = field(default_factory=dict)


# The MID sizing clause is verbatim the historical hardcoded prompt text, so the
# default tier reproduces prior behavior exactly.
_MID_SIZING = (
    "ONE deliverable (one endpoint OR one component OR one contract, ~1-3 files); "
    "each description names the exact target file(s) (matching the conventions) and "
    "the acceptance commands"
)

_TIERS: dict[str, TierProfile] = {
    HIGH: TierProfile(
        name=HIGH,
        sizing=(
            "a COHESIVE feature slice — it may span several closely-related "
            "endpoints/components/contracts (~3-8 files), because a high-capability "
            "owner can drive it with internally parallel subagents; keep each slice "
            "independently shippable and single-team, and name the target file(s) and "
            "acceptance commands"
        ),
        internal_parallelism=True,
        midrun_checks=False,
        thresholds={"max_subissues": 6, "max_children_per_parent": 4},
    ),
    MID: TierProfile(
        name=MID,
        sizing=_MID_SIZING,
        internal_parallelism=False,
        midrun_checks=False,
        thresholds={"max_subissues": 8, "max_children_per_parent": 5},
    ),
    REMEDIAL: TierProfile(
        name=REMEDIAL,
        sizing=(
            "the SMALLEST safely-shippable step — ONE function/route/file (~1 file). "
            "Prefer more, smaller issues over any issue with multiple parts, so a "
            "low-reliability worker cannot drift across a wide surface; name the exact "
            "target file and the acceptance commands"
        ),
        internal_parallelism=False,
        midrun_checks=True,
        thresholds={
            "max_subissues": 10,
            "max_children_per_parent": 6,
            "max_issues_per_goal": 40,
            "max_depth": 4,
            # One extra attempt before failing a low-reliability worker (forward
            # progress). Quarantine drift_threshold is deliberately NOT raised —
            # remedial surfaces drift via advisories, it does not halt more eagerly.
            "retry_cap": 4,
        },
    ),
}


def tier_names() -> list[str]:
    """The valid decomposition-tier names (bootstrap choices)."""
    return list(_TIERS)


def resolve_tier(name: str | None) -> TierProfile:
    """Resolve a tier name to its profile. Unknown/empty falls back to DEFAULT_TIER,
    so a mis-set project degrades to standard one-deliverable decomposition rather
    than failing to boot."""
    return _TIERS.get((name or "").strip().lower(), _TIERS[DEFAULT_TIER])

# A goal is "simple" when its text reads as a dependency bump / config tweak /
# rename rather than a new feature or subsystem. Such goals take the fast-path:
# exactly one implementation issue (the trivial verify is an acceptance criterion,
# not its own issue). Deliberately conservative — when unsure, decompose.
_SIMPLE_SIGNATURE = re.compile(
    r"\b(upgrade|bump|pin|downgrade|re-?pin|"
    r"depend(?:ency|encies)|dep\b|deps\b|"
    r"config\s+(?:tweak|change|update)|"
    r"rename|typo|version\s+bump|bump\s+version)\b",
    re.IGNORECASE,
)
# "<pkg> X→Y" / "X->Y" / "vN to vM" version-arrow shorthand.
_VERSION_ARROW = re.compile(r"\d+\s*(?:→|->|to)\s*\d+")
# A feature/subsystem ask is NOT simple even if it also says "update".
_FEATURE_SIGNATURE = re.compile(
    r"\b(add|build|create|implement|design|introduce|new)\b\s+"
    r"\w*\s*(feature|subsystem|service|endpoint|page|component|module|system|"
    r"pipeline|integration|dashboard|workflow)\b",
    re.IGNORECASE,
)

# Candidate sub-issues whose intent is just a QA gate the runner already owns.
# Anchored at the title start (the issue's primary intent) plus a few unambiguous
# noise phrases the spec calls out explicitly (UMD/CJS/ESM bundle output, HMR).
_QA_DUPLICATE = re.compile(
    r"^\s*(?:"
    r"tests?\b|testing\b|unit[\s-]?tests?\b|integration[\s-]?tests?\b|"
    r"run\s+(?:the\s+)?(?:unit\s+|integration\s+)?tests?\b|"
    r"add\s+(?:unit\s+|integration\s+)?tests?\b|write\s+tests?\b|"
    r"type[\s-]?check\w*|lint\w*|"
    r"e2e\b|end[\s-]?to[\s-]?end\b|playwright\b|cypress\b|"
    r"hmr\b|smoke[\s-]?test\w*|proxy[\s-]?config|error[\s-]?overlay|"
    r"bundle[\s-]?output|output[\s-]?format|esm[\s/]|verify\b|verification\b"
    r")",
    re.IGNORECASE,
)
# Library-only bundle-format concerns (nonsense for an application target).
_BUNDLE_FORMAT = re.compile(r"\b(esm|cjs|umd)\b.*\b(esm|cjs|umd)\b", re.IGNORECASE)


def is_simple_goal(title: str, description: str = "") -> bool:
    """True for dependency-bump / config-tweak style goals (the fast-path)."""
    text = f"{title}\n{description}"
    if _FEATURE_SIGNATURE.search(text):
        return False
    return bool(_SIMPLE_SIGNATURE.search(text) or _VERSION_ARROW.search(text))


def decompose_mode(flag: str | None, title: str, description: str = "") -> str:
    """Resolve the effective decomposition mode.

    Explicit override (goals.decompose) wins: 'single' | 'full'. Otherwise the
    simple-goal heuristic decides. Returns SINGLE or FULL.
    """
    if flag in (SINGLE, FULL):
        return flag
    return SINGLE if is_simple_goal(title, description) else FULL


def is_qa_duplicate(title: str) -> bool:
    """True if a candidate sub-issue merely duplicates a QA gate and must be
    dropped (verification belongs on the implementation issue, not its own issue)."""
    t = title or ""
    return bool(_QA_DUPLICATE.match(t) or _BUNDLE_FORMAT.search(t))


def drop_qa_duplicates(specs):
    """Filter a list of IssueSpec-likes, keeping only real implementation work."""
    return [s for s in specs if not is_qa_duplicate(getattr(s, "title", ""))]


def routing_violations(children, pipeline_team, resolve) -> list[str]:
    """Pure invariant check for a decomposed child set (spec §5.3, light form).

    `children` is a list of (issue_id, team) pairs; `resolve(team)` returns a
    roster team object (or None) — passed in so this stays I/O-free. Returns a
    list of human-readable violation strings (empty = OK):

      * a child's team does not resolve to a known roster team;
      * the pipeline declares a team and a child's team differs from it.
    """
    violations: list[str] = []
    for issue_id, team in children:
        if resolve(team) is None:
            violations.append(f"issue {issue_id}: team {team!r} is not a known team")
        if pipeline_team and team != pipeline_team:
            violations.append(
                f"issue {issue_id}: team {team!r} != pipeline team {pipeline_team!r}")
    return violations
