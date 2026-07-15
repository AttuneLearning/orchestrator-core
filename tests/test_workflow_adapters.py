"""Tests for workflow stack adapters: detection and default steps."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from orchestrator.workflow.adapters import (
    Adapter,
    detect_stack,
    get_adapter,
)
from orchestrator.workflow.adapters.node import NodeAdapter
from orchestrator.workflow.adapters.python import PythonAdapter
from orchestrator.workflow.adapters.golang import GoAdapter


class TestDetectStack:
    """Tests for detect_stack() stack detection logic."""

    def test_detect_node_stack(self, tmp_path):
        """Detect Node.js by package-lock.json."""
        (tmp_path / "package-lock.json").touch()
        assert detect_stack(tmp_path) == "node"

    def test_detect_python_poetry_stack(self, tmp_path):
        """Detect Python by poetry.lock."""
        (tmp_path / "poetry.lock").touch()
        assert detect_stack(tmp_path) == "python"

    def test_detect_python_uv_stack(self, tmp_path):
        """Detect Python by uv.lock."""
        (tmp_path / "uv.lock").touch()
        assert detect_stack(tmp_path) == "python"

    def test_detect_go_stack(self, tmp_path):
        """Detect Go by go.sum."""
        (tmp_path / "go.sum").touch()
        assert detect_stack(tmp_path) == "go"

    def test_detect_rust_stack(self, tmp_path):
        """Detect Rust by Cargo.lock."""
        (tmp_path / "Cargo.lock").touch()
        assert detect_stack(tmp_path) == "rust"

    def test_detect_empty_stack(self, tmp_path):
        """Return empty string when no lockfile present."""
        assert detect_stack(tmp_path) == ""

    def test_detect_precedence_node_wins(self, tmp_path):
        """package-lock.json wins over other lockfiles."""
        (tmp_path / "package-lock.json").touch()
        (tmp_path / "poetry.lock").touch()
        (tmp_path / "go.sum").touch()
        assert detect_stack(tmp_path) == "node"

    def test_detect_precedence_python_over_go(self, tmp_path):
        """poetry.lock wins over go.sum."""
        (tmp_path / "poetry.lock").touch()
        (tmp_path / "go.sum").touch()
        assert detect_stack(tmp_path) == "python"

    def test_detect_precedence_uv_over_go(self, tmp_path):
        """uv.lock wins over go.sum."""
        (tmp_path / "uv.lock").touch()
        (tmp_path / "go.sum").touch()
        assert detect_stack(tmp_path) == "python"


class TestGetAdapter:
    """Tests for get_adapter() adapter resolution."""

    def test_get_node_adapter(self):
        """Get NodeAdapter for 'node' stack."""
        adapter = get_adapter("node")
        assert adapter is not None
        assert isinstance(adapter, NodeAdapter)

    def test_get_python_adapter(self):
        """Get PythonAdapter for 'python' stack."""
        adapter = get_adapter("python")
        assert adapter is not None
        assert isinstance(adapter, PythonAdapter)

    def test_get_go_adapter(self):
        """Get GoAdapter for 'go' stack."""
        adapter = get_adapter("go")
        assert adapter is not None
        assert isinstance(adapter, GoAdapter)

    def test_get_no_adapter_empty(self):
        """Return None for empty stack."""
        adapter = get_adapter("")
        assert adapter is None

    def test_get_no_adapter_unknown(self):
        """Return None for unknown stack."""
        adapter = get_adapter("unknown")
        assert adapter is None

    def test_get_no_adapter_rust(self):
        """Return None for rust (not yet implemented)."""
        adapter = get_adapter("rust")
        assert adapter is None


class TestNodeAdapter:
    """Tests for Node adapter default steps and builtins."""

    def test_node_default_steps_shape(self):
        """Node adapter returns expected step structure."""
        adapter = NodeAdapter()
        steps = adapter.default_steps()

        assert "prepare" in steps
        assert "verify" in steps
        assert "cleanup" in steps

    def test_node_prepare_step(self):
        """prepare step uses node-deps-reconcile builtin."""
        adapter = NodeAdapter()
        steps = adapter.default_steps()

        prepare = steps["prepare"]
        assert len(prepare) == 1
        assert prepare[0]["builtin"] == "node-deps-reconcile"
        assert prepare[0]["on_fail"] == "escalate"
        assert prepare[0]["timeout"] == 300

    def test_node_verify_step(self):
        """verify step runs npm run typecheck && npm test."""
        adapter = NodeAdapter()
        steps = adapter.default_steps()

        verify = steps["verify"]
        assert len(verify) == 1
        assert verify[0]["run"] == "npm run typecheck && npm test"
        assert verify[0]["on_fail"] == "block"
        assert verify[0]["timeout"] == 300

    def test_node_cleanup_step(self):
        """cleanup step resets and cleans without -x."""
        adapter = NodeAdapter()
        steps = adapter.default_steps()

        cleanup = steps["cleanup"]
        assert len(cleanup) == 1
        # Check the exact command (no -x)
        assert cleanup[0]["run"] == "git reset --hard && git clean -fd"
        assert cleanup[0]["on_fail"] == "block"
        assert cleanup[0]["timeout"] == 300

    def test_node_cleanup_no_dash_x(self):
        """Verify no -x flag anywhere in cleanup."""
        adapter = NodeAdapter()
        steps = adapter.default_steps()
        cleanup_run = steps["cleanup"][0]["run"]
        assert "-x" not in cleanup_run

    def test_node_builtins(self):
        """Node adapter exports node-deps-reconcile builtin."""
        adapter = NodeAdapter()
        builtins = adapter.builtins()

        assert "node-deps-reconcile" in builtins
        assert callable(builtins["node-deps-reconcile"])

    def test_node_deps_reconcile_builtin(self, tmp_path):
        """node-deps-reconcile builtin returns ok dict with reason."""
        adapter = NodeAdapter()
        builtins = adapter.builtins()

        # Call with a tmp path (no package-lock.json -> should return ok=True)
        result = builtins["node-deps-reconcile"](tmp_path)

        assert isinstance(result, dict)
        assert "ok" in result
        assert "reason" in result
        assert result["ok"] is True  # No package-lock.json -> ok


class TestPythonAdapter:
    """Tests for Python adapter (stub for now)."""

    def test_python_default_steps_empty(self):
        """Python adapter returns empty dict (stub for WP-21)."""
        adapter = PythonAdapter()
        steps = adapter.default_steps()
        assert steps == {}

    def test_python_builtins_has_probe_tcp(self):
        """Python adapter inherits probe-tcp from base."""
        adapter = PythonAdapter()
        builtins = adapter.builtins()
        assert "probe-tcp" in builtins
        assert callable(builtins["probe-tcp"])


class TestGoAdapter:
    """Tests for Go adapter (stub for now)."""

    def test_go_default_steps_empty(self):
        """Go adapter returns empty dict (stub for future)."""
        adapter = GoAdapter()
        steps = adapter.default_steps()
        assert steps == {}

    def test_go_builtins_has_probe_tcp(self):
        """Go adapter inherits probe-tcp from base."""
        adapter = GoAdapter()
        builtins = adapter.builtins()
        assert "probe-tcp" in builtins
        assert callable(builtins["probe-tcp"])


class TestBaseAdapter:
    """Tests for base Adapter class."""

    def test_adapter_default_steps_empty(self):
        """Base Adapter.default_steps() returns empty dict."""
        adapter = Adapter()
        assert adapter.default_steps() == {}

    def test_adapter_builtins_has_probe_tcp(self):
        """Base Adapter.builtins() includes probe-tcp."""
        adapter = Adapter()
        builtins = adapter.builtins()
        assert "probe-tcp" in builtins
        assert callable(builtins["probe-tcp"])


class TestWorkflowYaml:
    """Tests for defaults/workflow.yaml file."""

    def test_workflow_yaml_loads(self):
        """defaults/workflow.yaml parses as valid YAML."""
        # Find the defaults/workflow.yaml file relative to the package
        import orchestrator.workflow
        pkg_dir = Path(orchestrator.workflow.__file__).parent
        workflow_yaml = pkg_dir.parent.parent / "defaults" / "workflow.yaml"

        assert workflow_yaml.is_file(), f"workflow.yaml not found at {workflow_yaml}"

        # Load and verify it parses
        content = workflow_yaml.read_text()
        profile = yaml.safe_load(content)

        assert isinstance(profile, dict)
        # Stack-neutral defaults: only cleanup (prepare/verify come from adapters)
        assert "cleanup" in profile
        assert "prepare" not in profile
        assert "verify" not in profile

    def test_workflow_yaml_structure(self):
        """defaults/workflow.yaml is stack-neutral with only cleanup."""
        import orchestrator.workflow
        pkg_dir = Path(orchestrator.workflow.__file__).parent
        workflow_yaml = pkg_dir.parent.parent / "defaults" / "workflow.yaml"

        content = workflow_yaml.read_text()
        profile = yaml.safe_load(content)

        # Stack-neutral defaults: only cleanup, no prepare/verify/stack
        assert "cleanup" in profile
        assert "prepare" not in profile
        assert "verify" not in profile
        assert "stack" not in profile

        # Check cleanup
        assert isinstance(profile["cleanup"], list)
        assert len(profile["cleanup"]) > 0
        assert "git reset --hard && git clean -fd" == profile["cleanup"][0]["run"]

    def test_workflow_yaml_no_dash_x(self):
        """defaults/workflow.yaml cleanup has no -x flag."""
        import orchestrator.workflow
        pkg_dir = Path(orchestrator.workflow.__file__).parent
        workflow_yaml = pkg_dir.parent.parent / "defaults" / "workflow.yaml"

        content = workflow_yaml.read_text()
        # Check raw text to ensure -x never appears
        assert "-x" not in content
