"""Off-rails: latch quarantine only when a mechanical signal fires AND the issue
has actually drifted from its goal (drift score below threshold).

This two-key rule prevents quarantining issues that are simply slow or noisy but
still on-track. Pure decision function; the loop performs the persistence.
"""

from __future__ import annotations


def should_quarantine(signals: list[str], drift_score: float, drift_threshold: float) -> bool:
    """True if at least one mechanical signal fired and drift is below threshold."""
    return bool(signals) and drift_score < drift_threshold
