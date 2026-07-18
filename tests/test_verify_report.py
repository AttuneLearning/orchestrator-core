"""Tests for structured verification parsing + classification.

Covers the false-negative-hardening contract: gate on test results, treat
non-zero-exit-with-zero-failures as a flake, and FAIL-SAFE to the exit code when
no report is available. See IMPL-PLAN-verify-false-negatives.md.
"""
from orchestrator import verify_report as vr


# ---- JUnit parsing ---------------------------------------------------------

def _write(tmp_path, text):
    p = tmp_path / "results.xml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_junit_all_pass(tmp_path):
    path = _write(tmp_path, '<testsuites><testsuite tests="10" failures="0" '
                            'errors="0" skipped="1"/></testsuites>')
    rep = vr.parse_report(path)
    assert rep is not None
    assert rep.tests == 10 and rep.failed == 0 and rep.suites_failed == 0


def test_parse_junit_with_failures(tmp_path):
    path = _write(tmp_path, '<testsuites>'
                            '<testsuite tests="5" failures="2" errors="0"/>'
                            '<testsuite tests="3" failures="0" errors="1"/>'
                            '</testsuites>')
    rep = vr.parse_report(path)
    assert rep.tests == 8 and rep.failed == 3 and rep.suites_failed == 2


def test_parse_single_testsuite_root(tmp_path):
    path = _write(tmp_path, '<testsuite tests="4" failures="0" errors="0"/>')
    rep = vr.parse_report(path)
    assert rep is not None and rep.tests == 4 and rep.failed == 0


def test_parse_missing_file_is_none():
    assert vr.parse_report("/no/such/report.xml") is None


def test_aggregate_directory_of_reports(tmp_path):
    # Monorepo: one JUnit file per workspace -> summed into one verdict.
    (tmp_path / "api.xml").write_text('<testsuite tests="1000" failures="0" errors="0"/>')
    (tmp_path / "web.xml").write_text('<testsuite tests="300" failures="0" errors="0"/>')
    (tmp_path / "contracts.xml").write_text('<testsuite tests="50" failures="0" errors="0"/>')
    rep = vr.parse_report(str(tmp_path))
    assert rep is not None and rep.tests == 1350 and rep.failed == 0


def test_aggregate_catches_failure_in_one_workspace(tmp_path):
    # The core reason aggregation matters: a failure in ANY workspace must count.
    (tmp_path / "api.xml").write_text('<testsuite tests="1000" failures="0" errors="0"/>')
    (tmp_path / "web.xml").write_text('<testsuite tests="300" failures="2" errors="0"/>')
    rep = vr.parse_report(str(tmp_path))
    assert rep.tests == 1300 and rep.failed == 2 and rep.suites_failed == 1
    passed, cls = vr.classify(0, rep, vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is False and cls == "failed"


def test_aggregate_glob(tmp_path):
    (tmp_path / "a.xml").write_text('<testsuite tests="5" failures="0"/>')
    (tmp_path / "b.xml").write_text('<testsuite tests="7" failures="0"/>')
    rep = vr.parse_report(str(tmp_path / "*.xml"))
    assert rep.tests == 12


def test_empty_directory_is_none(tmp_path):
    assert vr.parse_report(str(tmp_path)) is None


def test_parse_empty_path_is_none():
    assert vr.parse_report("") is None


def test_parse_garbage_is_none(tmp_path):
    path = _write(tmp_path, "not xml at all <<<")
    assert vr.parse_report(path) is None


def test_parse_unknown_format_is_none(tmp_path):
    path = _write(tmp_path, '<testsuite tests="1"/>')
    assert vr.parse_report(path, fmt="vitest-json") is None


# ---- classify: the decision matrix -----------------------------------------

def _rep(failed=0, suites_failed=0, unhandled=None):
    return vr.VerifyReport(tests=10, failures=failed, suites_failed=suites_failed,
                           unhandled=unhandled or [])


def test_classify_clean():
    passed, cls = vr.classify(0, _rep(), vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is True and cls == "clean"


def test_classify_real_test_failure():
    passed, cls = vr.classify(1, _rep(failed=1, suites_failed=1), vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is False and cls == "failed"


def test_classify_flaky_exit_green_tests_nonzero_exit():
    # The core case: all tests pass but the process exits non-zero -> flake.
    passed, cls = vr.classify(1, _rep(failed=0), vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is None and cls == "flaky_exit"


def test_classify_ignored_unhandled_is_not_a_failure():
    rep = _rep(unhandled=["Error: Not implemented: navigation (except hash changes)"])
    passed, cls = vr.classify(1, rep, vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is None and cls == "flaky_exit"  # ignored -> not a failure


def test_classify_unlisted_unhandled_still_fails():
    rep = _rep(unhandled=["Error: real unhandled rejection in prod code"])
    passed, cls = vr.classify(1, rep, vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is False and cls == "failed"


def test_classify_typecheck_failure():
    passed, cls = vr.classify(0, _rep(), vr.DEFAULT_IGNORE_UNHANDLED, typecheck_ok=False)
    assert passed is False and cls == "typecheck_failed"


def test_classify_failsafe_no_report_exit0():
    passed, cls = vr.classify(0, None, vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is True and cls == "clean"


def test_classify_failsafe_no_report_nonzero():
    # No structured evidence -> trust the exit code (today's behavior), never flake.
    passed, cls = vr.classify(2, None, vr.DEFAULT_IGNORE_UNHANDLED)
    assert passed is False and cls == "failed_no_report"


def test_matches_any():
    assert vr.matches_any("Not implemented: navigation", vr.DEFAULT_IGNORE_UNHANDLED)
    assert not vr.matches_any("some other error", vr.DEFAULT_IGNORE_UNHANDLED)
