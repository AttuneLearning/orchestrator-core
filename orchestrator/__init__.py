"""Autonomous multi-agent orchestrator (python-orchestrator-v1).

Plain-Python orchestrator with canonical state in Postgres. Agents act through
an MCP tool layer; a single-threaded engine loop drives issues through the
five-phase pipeline #1 defined in config/pipelines.yaml.
"""

__version__ = "0.1.0"
