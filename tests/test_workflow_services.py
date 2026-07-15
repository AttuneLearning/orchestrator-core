"""Tests for service readiness probes (WP-18).

Pure tests: no network dependencies, uses socket.bind on port 0 to get a
listening socket for success cases, and closed ports for failure cases.

The builtin contract is `fn(worktree, action) -> dict`: `probe-tcp` is an
ordinary registry entry (no prefix-matching, no contextvar) and reads its
endpoint spec from `action.args` ("service=host:port" or bare "host:port").
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import yaml

from orchestrator.workflow import adapters, loader
from orchestrator.workflow.models import Profile, RequiredAction, WorkflowStep
from orchestrator.workflow.permissions import Permissions
from orchestrator.workflow.runner import StepResult, run_step


# ---------------------------------------------------------------------------
# Builtin probe-tcp tests
# ---------------------------------------------------------------------------


class TestProbeTcp:
    """Tests for the probe-tcp builtin handler."""

    def test_probe_tcp_success_with_listening_socket(self, tmp_path: Path) -> None:
        """Probe succeeds when connecting to a listening socket."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))  # port 0 = OS picks one
            listener.listen(1)
            host, port = listener.getsockname()
            endpoint = f"{host}:{port}"

            adapter = adapters.Adapter()
            probe_fn = adapter.builtins()["probe-tcp"]
            action = RequiredAction(builtin="probe-tcp", args=f"test={endpoint}")
            result = probe_fn(tmp_path, action)

            assert result["ok"] is True
            assert endpoint in result["reason"]

    def test_probe_tcp_bare_host_port_no_service_name(self, tmp_path: Path) -> None:
        """A bare 'host:port' (no 'service=' prefix) is also accepted."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
            endpoint = f"{host}:{port}"

            adapter = adapters.Adapter()
            probe_fn = adapter.builtins()["probe-tcp"]
            action = RequiredAction(builtin="probe-tcp", args=endpoint)
            result = probe_fn(tmp_path, action)

            assert result["ok"] is True

    def test_probe_tcp_failure_closed_port(self, tmp_path: Path) -> None:
        """Probe fails when port is closed."""
        endpoint = "127.0.0.1:54321"

        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp", args=f"test={endpoint}")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "54321" in result["reason"]

    def test_probe_tcp_malformed_endpoint_missing_port(self, tmp_path: Path) -> None:
        """Probe fails gracefully with a malformed endpoint (missing port)."""
        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp", args="test=localhost")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "malformed" in result["reason"] or ":" in result["reason"]

    def test_probe_tcp_malformed_endpoint_empty_after_equals(self, tmp_path: Path) -> None:
        """Probe fails gracefully when args is 'mongo=' (empty endpoint)."""
        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp", args="mongo=")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "malformed" in result["reason"]

    def test_probe_tcp_malformed_endpoint_no_equals_no_colon(self, tmp_path: Path) -> None:
        """Probe fails gracefully when args is just 'mongo' (no '=', no ':')."""
        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp", args="mongo")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "malformed" in result["reason"]

    def test_probe_tcp_malformed_endpoint_non_numeric_port(self, tmp_path: Path) -> None:
        """Probe fails gracefully when the port isn't a number."""
        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp", args="mongo=host:notaport")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "malformed" in result["reason"]

    def test_probe_tcp_never_raises_on_empty_args(self, tmp_path: Path) -> None:
        """An action with no args at all degrades cleanly, never crashes."""
        adapter = adapters.Adapter()
        probe_fn = adapter.builtins()["probe-tcp"]
        action = RequiredAction(builtin="probe-tcp")
        result = probe_fn(tmp_path, action)

        assert result["ok"] is False
        assert "reason" in result

    def test_probe_tcp_no_leakage_between_calls(self, tmp_path: Path) -> None:
        """A bare/second probe never reads a prior endpoint (regression test
        for the removed contextvar/prefix-dict scheme: state must never leak
        from one call to the next, since there is no shared context now)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
            good_endpoint = f"{host}:{port}"

            adapter = adapters.Adapter()
            probe_fn = adapter.builtins()["probe-tcp"]

            # First call: a real, listening endpoint -> ok=True.
            first = probe_fn(tmp_path, RequiredAction(builtin="probe-tcp", args=f"svc1={good_endpoint}"))
            assert first["ok"] is True

            # Second call: bare builtin with NO args at all. If any state ever
            # leaked from the first call, this would wrongly probe
            # good_endpoint and succeed; it must instead fail cleanly.
            second = probe_fn(tmp_path, RequiredAction(builtin="probe-tcp"))
            assert second["ok"] is False
            assert good_endpoint not in second["reason"]


# ---------------------------------------------------------------------------
# Loader services expansion tests
# ---------------------------------------------------------------------------


class TestServicesExpansion:
    """Tests for expanding services: [list] into probe actions."""

    def test_expand_services_none_returns_unchanged(self) -> None:
        """Profile with no services is returned unchanged."""
        profile = Profile(stack="node", services=())
        expanded = loader._expand_services_step(profile)
        assert expanded.services == ()
        assert "services" not in expanded.steps

    def test_expand_services_with_defaults(self) -> None:
        """Services expanded into probe actions with default endpoints."""
        profile = Profile(stack="node", services=("mongo", "redis"))
        expanded = loader._expand_services_step(profile)

        assert "services" in expanded.steps
        services_step = expanded.steps["services"]
        assert len(services_step.actions) == 2

        actions = services_step.actions
        assert actions[0].builtin == "probe-tcp"
        assert actions[0].args == "mongo=localhost:27017"
        assert actions[0].on_fail == "escalate"
        assert actions[0].timeout == 5
        assert actions[1].builtin == "probe-tcp"
        assert actions[1].args == "redis=localhost:6379"

    def test_expand_services_with_workspace_override(self) -> None:
        """Workspace service_endpoints override defaults."""
        profile = Profile(stack="node", services=("mongo", "redis"))
        overrides = {"mongo": "10.0.0.5:27017", "redis": "10.0.0.6:6379"}
        expanded = loader._expand_services_step(profile, overrides)

        actions = expanded.steps["services"].actions
        assert actions[0].builtin == "probe-tcp"
        assert actions[0].args == "mongo=10.0.0.5:27017"
        assert actions[1].args == "redis=10.0.0.6:6379"

    def test_expand_services_partial_override(self) -> None:
        """Partial overrides mix defaults and workspace values."""
        profile = Profile(stack="node", services=("mongo", "redis"))
        overrides = {"mongo": "custom:1234"}
        expanded = loader._expand_services_step(profile, overrides)

        actions = expanded.steps["services"].actions
        assert actions[0].args == "mongo=custom:1234"
        assert actions[1].args == "redis=localhost:6379"

    def test_expand_services_s3_mock(self) -> None:
        """Services expansion includes s3-mock default port."""
        profile = Profile(stack="node", services=("s3-mock",))
        expanded = loader._expand_services_step(profile)

        actions = expanded.steps["services"].actions
        assert actions[0].args == "s3-mock=localhost:9000"

    def test_expand_services_postgres(self) -> None:
        """Services expansion includes postgres default port."""
        profile = Profile(stack="node", services=("postgres",))
        expanded = loader._expand_services_step(profile)

        actions = expanded.steps["services"].actions
        assert actions[0].args == "postgres=localhost:5432"


# ---------------------------------------------------------------------------
# Loader integration tests
# ---------------------------------------------------------------------------


class TestLoaderServicesIntegration:
    """Integration tests for services in load_effective."""

    def test_load_effective_with_services_scalar_in_yaml(self, tmp_path: Path) -> None:
        """load_effective expands services: [list] from YAML."""
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            yaml.dump(
                {
                    "services": ["mongo", "redis"],
                    "permissions": {"allow": [], "deny": []},
                }
            )
        )

        class MockSettings:
            workspace_manifest = str(manifest)

        settings = MockSettings()
        profile = loader.load_effective(settings, tmp_path)

        assert "services" in profile.steps
        assert len(profile.steps["services"].actions) == 2
        assert all(a.builtin == "probe-tcp" for a in profile.steps["services"].actions)

    def test_load_effective_with_service_endpoints_override(self, tmp_path: Path) -> None:
        """Workspace service_endpoints override defaults."""
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            yaml.dump(
                {
                    "services": ["mongo"],
                    "service_endpoints": {"mongo": "db.example.com:27017"},
                    "permissions": {"allow": [], "deny": []},
                }
            )
        )

        class MockSettings:
            workspace_manifest = str(manifest)

        settings = MockSettings()
        profile = loader.load_effective(settings, tmp_path)

        action = profile.steps["services"].actions[0]
        assert action.builtin == "probe-tcp"
        assert action.args == "mongo=db.example.com:27017"

    def test_load_effective_services_from_repo_layer(self, tmp_path: Path) -> None:
        """Repo layer services: scalar is expanded."""
        repo_profile_dir = tmp_path / ".orchestrator"
        repo_profile_dir.mkdir()
        repo_profile = repo_profile_dir / "workflow.yaml"
        repo_profile.write_text(
            yaml.dump(
                {
                    "services": ["postgres"],
                }
            )
        )

        class MockSettings:
            workspace_manifest = ""

        settings = MockSettings()
        profile = loader.load_effective(settings, tmp_path)

        assert "services" in profile.steps
        action = profile.steps["services"].actions[0]
        assert action.args == "postgres=localhost:5432"

    def test_load_effective_repo_service_endpoints_ignored_with_warning(
        self, tmp_path: Path
    ) -> None:
        """Repo layer service_endpoints are ignored with a warning."""
        repo_profile_dir = tmp_path / ".orchestrator"
        repo_profile_dir.mkdir()
        repo_profile = repo_profile_dir / "workflow.yaml"
        repo_profile.write_text(
            yaml.dump(
                {
                    "services": ["mongo"],
                    "service_endpoints": {"mongo": "evil.example.com:27017"},
                }
            )
        )

        class MockSettings:
            workspace_manifest = ""

        settings = MockSettings()
        profile = loader.load_effective(settings, tmp_path)

        # Default endpoint is used (repo's override ignored).
        action = profile.steps["services"].actions[0]
        assert action.args == "mongo=localhost:27017"

        assert any("service_endpoints" in w for w in profile.warnings)


# ---------------------------------------------------------------------------
# End-to-end runner tests
# ---------------------------------------------------------------------------


class TestServicesRunnerE2E:
    """End-to-end tests via run_step."""

    def test_run_services_step_success(self, tmp_path: Path) -> None:
        """run_step succeeds when all probes connect."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as mongo_sock:
            mongo_sock.bind(("127.0.0.1", 0))
            mongo_sock.listen(1)
            mongo_host, mongo_port = mongo_sock.getsockname()

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as redis_sock:
                redis_sock.bind(("127.0.0.1", 0))
                redis_sock.listen(1)
                redis_host, redis_port = redis_sock.getsockname()

                mongo_endpoint = f"{mongo_host}:{mongo_port}"
                redis_endpoint = f"{redis_host}:{redis_port}"

                actions = (
                    RequiredAction(
                        builtin="probe-tcp",
                        args=f"mongo={mongo_endpoint}",
                        on_fail="block",
                        timeout=5,
                    ),
                    RequiredAction(
                        builtin="probe-tcp",
                        args=f"redis={redis_endpoint}",
                        on_fail="block",
                        timeout=5,
                    ),
                )
                profile = Profile(
                    stack="node",
                    steps={"services": WorkflowStep(name="services", actions=actions)},
                )

                events, event_cb = _collector()
                result = run_step(tmp_path, profile, "services", None, Permissions(), event_cb=event_cb)

                assert result.status == "ok"
                assert len(result.results) == 2
                assert all(r.ok for r in result.results)
                assert len(events) == 2
                assert all(kind == "executed" for kind, _ in events)

    def test_run_services_step_failure(self, tmp_path: Path) -> None:
        """run_step fails when a probe cannot connect."""
        actions = (
            RequiredAction(
                builtin="probe-tcp",
                args="mongo=127.0.0.1:54321",
                on_fail="block",
                timeout=5,
            ),
        )
        profile = Profile(
            stack="node",
            steps={"services": WorkflowStep(name="services", actions=actions)},
        )

        events, event_cb = _collector()
        result = run_step(tmp_path, profile, "services", None, Permissions(), event_cb=event_cb)

        assert result.status == "failed"
        assert len(result.results) == 1
        assert result.results[0].ok is False
        assert len(events) == 1
        assert events[0][0] == "failed"

    def test_run_services_step_escalate_on_fail(self, tmp_path: Path) -> None:
        """on_fail=escalate blocks the step on probe failure (no custom
        escalation_cb wired here — mirrors run_step's own default, distinct
        from the call sites' deny-by-default wiring in tools_issues.py/
        worktree.py)."""
        actions = (
            RequiredAction(
                builtin="probe-tcp",
                args="mongo=127.0.0.1:54321",
                on_fail="escalate",
                timeout=5,
                source="default",
            ),
        )
        profile = Profile(
            stack="node",
            steps={"services": WorkflowStep(name="services", actions=actions)},
        )

        result = run_step(tmp_path, profile, "services", None, Permissions())

        assert result.status == "blocked_on_approval"
        assert len(result.results) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collector():
    """An event_cb that records (kind, payload) tuples for assertions."""
    events = []

    def cb(kind: str, payload: dict) -> None:
        json.dumps(payload)
        events.append((kind, payload))

    return events, cb
