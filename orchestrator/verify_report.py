"""Structured verification-result parsing + classification (framework-wide).

The verify harness must gate on TEST RESULTS, not the raw process exit code: a
flaky non-zero exit with zero failed tests (jsdom unhandled navigation, a
teardown crash, a warning-as-error, a stray timer) must not false-decline a
green deliverable. Every major runner emits JUnit XML natively, so one parser
covers vitest/jest/pytest/go. Config stays YAML; the *report* is JUnit XML
because no runner emits YAML.

Design contract (see IMPL-PLAN-verify-false-negatives.md):
  - passed True/False  -> decisive.
  - passed None        -> "flaky_exit": tests green but the process exited
                          non-zero. The caller retries once, then flags a human
                          (never a silent decline, never a retry-budget burn).
  - FAIL-SAFE: a missing/unparseable report falls back to the exit code, i.e.
    exactly today's behavior — never fail-open.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

# Known-harmless async runtime noise that must never fail an otherwise-green run.
# Narrow + explicit: anything NOT matched here still fails. Instances extend this
# via settings.verify_ignore_unhandled (merged, not replaced).
DEFAULT_IGNORE_UNHANDLED: tuple[str, ...] = (
    r"Not implemented: navigation",        # jsdom hard navigation from a <Link>/<a> click
    r"Not implemented: window\.scrollTo",
    r"ResizeObserver loop",
)


@dataclass
class VerifyReport:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    suites_failed: int = 0
    # Unhandled/process-level errors the runner surfaced (populated by formats
    # that carry them, e.g. vitest-json; empty for plain JUnit).
    unhandled: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.failures + self.errors


def matches_any(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in (patterns or []))


def parse_report(path: str, fmt: str = "junit") -> "VerifyReport | None":
    """Parse a machine test report. Returns None when the report is absent or
    unparseable so the caller can FAIL-SAFE to the exit code (never fail-open)."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return None
    fmt = (fmt or "junit").lower()
    try:
        if fmt == "junit":
            return _parse_junit(data)
        # Phase 2: "vitest-json" / "pytest-json" / "gotest-json" plug in here and
        # can populate .unhandled. Unknown format -> None (fail-safe).
        return None
    except Exception:
        return None


def _parse_junit(data: str) -> "VerifyReport | None":
    if not data.strip():
        return None
    root = ET.fromstring(data)
    if root.tag == "testsuite":
        suites = [root]
    else:  # <testsuites> wrapper, or any root that contains <testsuite> nodes
        suites = list(root.iter("testsuite"))
    if not suites:
        return None
    rep = VerifyReport()
    for s in suites:
        f = int(s.get("failures", "0") or 0)
        e = int(s.get("errors", "0") or 0)
        rep.tests += int(s.get("tests", "0") or 0)
        rep.failures += f
        rep.errors += e
        rep.skipped += int(s.get("skipped", "0") or 0)
        if f or e:
            rep.suites_failed += 1
    return rep


def classify(returncode: int, report: "VerifyReport | None", ignore_patterns,
             typecheck_ok: bool = True) -> tuple["bool | None", str]:
    """Decide the verdict from the structured report, not the exit code.

    Returns (passed, classification):
      (False, 'typecheck_failed')  typecheck step failed (no tests ran)
      (bool,  'clean'|'failed_no_report')  FAIL-SAFE: no report -> trust exit code
      (False, 'failed')            real test failure or un-ignored unhandled error
      (True,  'clean')             all tests green, exit 0
      (None,  'flaky_exit')        all tests green but non-zero exit -> flake
    """
    if not typecheck_ok:
        return False, "typecheck_failed"
    if report is None:
        # No structured evidence -> fall back to exit code (today's behavior).
        return (returncode == 0), ("clean" if returncode == 0 else "failed_no_report")
    real_unhandled = [u for u in report.unhandled
                      if not matches_any(u, ignore_patterns)]
    if report.failed > 0 or report.suites_failed > 0 or real_unhandled:
        return False, "failed"
    if returncode == 0:
        return True, "clean"
    return None, "flaky_exit"
