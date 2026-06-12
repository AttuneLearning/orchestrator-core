"""MCP server: the single tool interface through which agents act.

Every tool wraps repository.py — the same code path the engine uses — so agent
actions land in the append-only issue_events log that off-rails detection relies
on. Agents never write to Postgres except through these tools.
"""
