"""Multi-coordinator dashboard: daemon-heartbeat liveness, the project dropdown,
and ?project= threading through links/forms."""

from orchestrator import repository as repo
from orchestrator.dashboard import context, templates
from orchestrator.dashboard.instances import Instance, Registry


def test_daemon_heartbeat_age(pool):
    assert repo.daemon_heartbeat_age_seconds(pool) is None     # never ticked
    repo.record_daemon_heartbeat(pool)
    age = repo.daemon_heartbeat_age_seconds(pool)
    assert age is not None and age < 5                          # fresh, by DB clock


def test_registry_liveness_reflects_heartbeat(pool, settings):
    inst = Instance("a", "Alpha", settings, pool=pool)
    assert Registry({"a": inst}, "a").liveness("a") == "idle"   # reachable, no tick
    repo.record_daemon_heartbeat(pool)
    assert Registry({"a": inst}, "a").liveness("a") == "live"   # fresh registry/cache


def test_page_threads_project_and_shows_picker(pool, settings):
    reg = Registry({"a": Instance("a", "Alpha", settings, pool=pool),
                    "b": Instance("b", "Beta", settings, pool=pool)}, "a")
    context.install_registry(reg)
    token = context.set_current(reg.get("b"))                   # non-default coordinator
    try:
        html = templates.page("t", "<a href='/goals/1'>g</a><form action='/x'></form>")
        assert "/goals/1?project=b" in html        # internal link threaded
        assert "/x?project=b" in html              # form action threaded
        assert "<select" in html                   # picker rendered (2 coordinators)
        assert "Alpha" in html and "Beta" in html
    finally:
        context.reset_current(token)
        context.install_registry(None)


def test_default_coordinator_keeps_clean_urls(pool, settings):
    reg = Registry({"a": Instance("a", "Alpha", settings, pool=pool)}, "a")
    context.install_registry(reg)
    token = context.set_current(reg.get("a"))
    try:
        html = templates.page("t", "<a href='/goals/1'>g</a>")
        assert "project=" not in html              # default → clean URLs
        assert "<select" not in html               # one coordinator → no picker
    finally:
        context.reset_current(token)
        context.install_registry(None)
